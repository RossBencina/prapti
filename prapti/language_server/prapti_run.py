"""
Code that actually runs prapti in the language server context.
Includes streaming text inserter setup.
"""
# TODO: this file needs major cleanup
import asyncio
from typing import Callable
from enum import Enum
import sys

from cancel_token import CancellationToken
from pygls.server import LanguageServer
from pygls.workspace import TextDocument, PositionCodec
from lsprotocol.types import (
    MessageType,
    Position,
)

from .observable_lsp import ObservableLanguageServerProtocol
from .inserter import TextInserter, insert_queued_text
from .inserter import QueueSentinel as TextInserterQueueSentinel
import prapti.tool

# -----------------------------------------------------------------------------------------

def make_end_of_file_position(document_text: str|None, position_codec: PositionCodec) -> Position:
    # NOTE: works with "\n" or "\r\n" EOL markers
    if not document_text:
        return Position(line=0, character=0)
    elif document_text.endswith("\n"):
        return Position(line=document_text.count("\n"), character=0)
    return Position(line=document_text.count("\n"), character=position_codec.client_num_units(document_text.rsplit("\n", 1)[-1]))

# -----------------------------------------------------------------------------------------

# use document-consistent line endings when inserting text into document

# When inserting text into the document, ensure that the correct line ending sequence is used. This requires determining
# the line ending sequence, for example by inspecting existing line endings (current implementation) and/or reading
# .editorconfig (probably what we should do).

# I have asked about current best practice here:
# https://stackoverflow.com/questions/77262975/how-should-a-language-server-protocol-server-determine-the-correct-end-of-line-s

# ?does lsp tell the server the current LF/CRLF mode for document? unclear, but probably not, see:
#   "Clarify how a server can know which line ending sequence to use (LF or CRLF) when inserting text"
#    https://github.com/microsoft/language-server-protocol/issues/1800

# REVIEW: if there's a more robust way to get the editor line ending style from within VSCode we could grab it
# prior to invoking the language server command.

# TODO: Ensure that when ingesting document lines for processing by prapti, that we normalize trailng "\r\n" to "\n"

# NOTE: as of 10/10/23 prapti itself doesn't respect the file's line ending style, it just reads/writes '\n' line endings and
# expects text-mode file io to translate.

def detect_eol_sequence(source: str):
    """Returns the end of line sequence based on the document source text or native EOL mode"""
    if "\r\n" in source:
        return "\r\n"
    elif "\n" in source:
        return "\n"
    else:
        # HACK fallback if document contains no end of line characters (e.g. empty document)
        return os.linesep
        # REVIEW: could maybe use "\n" always if the client performs normalization (? see notes above)
        # REVIEW: could/should consult .editorconfig instead of using os.linesep default

def rewrite_eol_sequence_to_lf(s: str) -> str:
    if "\r\n" in s:
        return s.replace("\r\n", "\n")
    return s

def rewrite_eol_sequence_to_crlf(s: str) -> str:
    if "\n" in s:
        if "\r\n" in s:
            return s.replace("\r\n", "\n").replace("\n", "\r\n") # idempotent in the presence of "\r\n"
        else:
            return s.replace("\n", "\r\n")
    else:
        return s

def select_eol_sequence_rewriter(eol_sequence: str) -> Callable[[str], str]:
    return rewrite_eol_sequence_to_lf if eol_sequence == "\n" else rewrite_eol_sequence_to_crlf

# -----------------------------------------------------------------------------------------

async def run_prapti_text_generation(ls: LanguageServer, document: TextDocument, cancellation_token: CancellationToken, rewrite_eol_sequence, queue: asyncio.Queue):
    """Run prapti and push generated text into the queue."""

    try:
        # TODO: somehow re-route log messages as diagnostics and/or lsp log messages

        # REVIEW: pygls makes a reasonable attempt to convert document.uri into a local
        # file system path stored in document.path. this will only work well for `file://` uri scheme
        # if we want to support unsaved documents and liveshare documents
        # we will probably need to work harder and use urllib.parse
        input_filepath = document.path
        run_phase_1_on_separate_thread = False

        ls.show_message_log(f"starting phase 1", msg_type=MessageType.Log)
        sys.stderr.flush() # flush log. doesn't seem to work :/
        # sync input processing:

        if run_phase_1_on_separate_thread:
            # FIXME
            # Python 3.10.11
            # running in a separate thread appears to be stalling, not sure why
            # seems to be stalling while importing numpy in
            # C:\Python310\Lib\site-packages\openai\datalib\numpy_helper.py
            # while importing openai in the openai_chat_responder.py plugin
            # importing numpy earlier in the thread does not cause the hang, so possibly
            # it's some kind of race

            run_state = await asyncio.to_thread(prapti.tool.run_phase_1, argv=["prapti", input_filepath], input_lines=document.lines)
        else:
            run_state = prapti.tool.run_phase_1(argv=["prapti", input_filepath], input_lines=document.lines)

        sys.stderr.flush() # flush log. doesn't seem to work :/
        ls.show_message_log(f"phase 1 completed", msg_type=MessageType.Log)

        if cancellation_token.cancelled:
            return

        # async response generation:
        try:
            async_output = prapti.tool.run_phase_2(run_state, cancellation_token)
            async for item in async_output:
                match item:
                    case str():
                        queue.put_nowait(rewrite_eol_sequence(item))
                    case prapti.tool.EndOfOutputSentinel():
                        pass
                    case prapti.tool.CompletionSentinel():
                        # could log result code here:
                        ls.show_message_log(f"Prapti run completed with result code {item.result_code}", msg_type=MessageType.Log)
                        return
            assert False, "run_phase_2 did not yield CompletionSentinel"
        except asyncio.CancelledError:
            pass
    finally:
        queue.put_nowait(TextInserterQueueSentinel.END_OF_STREAM)

# -----------------------------------------------------------------------------------------

class LsPraptiRunState(Enum):
    INITIALISED = 0
    RUNNING = 1
    STOPPING = 2
    STOPPED = 3

class LsPraptiRun:
    def __init__(self):
        self.inserter = None
        self.state = LsPraptiRunState.INITIALISED

    async def run(self, ls: LanguageServer, lsp: ObservableLanguageServerProtocol, document: TextDocument):
        insertion_pos = make_end_of_file_position(document.source, document.position_codec)

        eol_sequence = detect_eol_sequence(document.source)
        eol_sequence_rewriter = select_eol_sequence_rewriter(eol_sequence)

        self.output_task = None
        self.cancellation_token = None
        self.enqueue_task = None

        queue: asyncio.Queue[str|TextInserterQueueSentinel] = asyncio.Queue(maxsize=0) # unbounded queue, buffers text generated by prapti

        self.inserter = TextInserter(ls, document, insertion_pos, eol_sequence, queue)
        lsp.register_observer(document.uri, self.inserter)
        try:
            self.output_task = asyncio.create_task(insert_queued_text(queue, self.inserter)) # push queued text into document

            self.cancellation_token = CancellationToken()
            self.enqueue_task = asyncio.create_task(run_prapti_text_generation(ls, document, self.cancellation_token, eol_sequence_rewriter, queue)) # pump text generated by prapti into the queue
            self.state = LsPraptiRunState.RUNNING
            await asyncio.gather(self.enqueue_task, self.output_task)
        finally:
            # TODO: clean up gracefully, e.g. try to cancel any running tasks, with a small timeout.
            lsp.unregister_observer(document.uri, self.inserter)
            self.inserter = None
            self.output_task = None
            self.cancellation_token = None
            self.enqueue_task = None
            self.state = LsPraptiRunState.STOPPED

    async def cancel(self):
        # cancel the run by cancelling enqueue_task. this causes EndOfStream to be
        # pushed onto the queue
        # and all downstream operations will complete cleanly

        if self.state != LsPraptiRunState.RUNNING:
            return
        self.state = LsPraptiRunState.STOPPING # FIXME this is a race with the assignment to STOPPED in run()
        self.cancellation_token.cancel()
