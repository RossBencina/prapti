"""
Code that actually runs prapti in the language server context.
Includes streaming text inserter setup.
"""
# TODO: this file needs major cleanup
import asyncio
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
from .line_endings import detect_eol_sequence, select_line_endings_rewriter

import prapti.tool

# ----------------------------------------------------------------------------

def make_end_of_file_position(document_text: str|None, position_codec: PositionCodec) -> Position:
    # NOTE: works with both "\n" and "\r\n" line ending sequences
    if not document_text:
        return Position(line=0, character=0)
    elif document_text.endswith("\n"):
        return Position(line=document_text.count("\n"), character=0)
    return Position(line=document_text.count("\n"), character=position_codec.client_num_units(document_text.rsplit("\n", 1)[-1]))

# ----------------------------------------------------------------------------

async def run_prapti_text_generation(ls: LanguageServer, document: TextDocument, cancellation_token: CancellationToken, rewrite_line_endings, queue: asyncio.Queue):
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
                        queue.put_nowait(rewrite_line_endings(item))
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

# ----------------------------------------------------------------------------

class LsPraptiRunState(Enum):
    INITIALISED = 0
    RUNNING = 1
    CANCELLING = 2
    STOPPED = 3

class LsPraptiRun:
    def __init__(self):
        self._cancellation_token = CancellationToken()
        self.state = LsPraptiRunState.INITIALISED

    async def run(self, ls: LanguageServer, lsp: ObservableLanguageServerProtocol, document: TextDocument):
        assert self.state == LsPraptiRunState.INITIALISED # self is a single-use object

        insertion_pos = make_end_of_file_position(document.source, document.position_codec)

        # FIXME: document-sensitive line ending style handling should be part of core tool, not language server
        eol_sequence = detect_eol_sequence(document.source)
        line_endings_rewriter = select_line_endings_rewriter(eol_sequence)

        queue: asyncio.Queue[str|TextInserterQueueSentinel] = asyncio.Queue(maxsize=0) # unbounded queue, buffers text generated by prapti

        inserter = TextInserter(ls, document, insertion_pos, eol_sequence, queue)
        lsp.register_observer(document.uri, inserter)
        try:
            dequeue_task = asyncio.create_task(insert_queued_text(queue, inserter)) # push queued text into inserter, then into document
            enqueue_task = asyncio.create_task(run_prapti_text_generation(ls, document, self._cancellation_token, line_endings_rewriter, queue)) # pump text generated by prapti into queue
            self.state = LsPraptiRunState.RUNNING
            await asyncio.gather(enqueue_task, dequeue_task, return_exceptions=True)
        finally:
            lsp.unregister_observer(document.uri, inserter)
            self._cancellation_token.complete()
            self.state = LsPraptiRunState.STOPPED

    async def cancel(self):
        if self.state != LsPraptiRunState.RUNNING:
            return
        self.state = LsPraptiRunState.CANCELLING

        # cancel the run by cancelling the prapti generation run. this causes EndOfStream to be
        # pushed onto the queue and all downstream operations will complete cleanly
        self._cancellation_token.cancel()
