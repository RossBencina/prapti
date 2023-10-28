import asyncio
from typing import AsyncGenerator
from dataclasses import dataclass
from enum import Enum
import copy
import re

from pygls.server import LanguageServer
from pygls.workspace import TextDocument, PositionCodec

from lsprotocol.types import (
    MessageType,
    Position,
    Range,
    WorkspaceEdit,
    TextEdit,
    TextDocumentEdit,
    OptionalVersionedTextDocumentIdentifier,
    TextDocumentContentChangeEvent_Type1
)

from .observable_lsp import TextDocumentObserver, TextDocumentContentChangeTransaction

# TODO: make cursor char a parameter
CURSOR_CHAR = "\u2588" # Full block █ (U+2588)
CURSOR_CHAR_PATTERN = re.compile(CURSOR_CHAR)

@dataclass
class CursorDescription:
    position: Position  # cursor position. this is the insertion point, and the position of cursor char if present
                        # in lsp client format (e.g. utf16) *NOT* in Python format
    has_cursor_char: bool # CURSOR_CHAR is present at position
    has_eol: bool # "\n" or "\r\n" is present directly after CURSOR_CHAR

@dataclass
class CursorUpdate:
    from_document_version: int
    to_document_version: int
    text_edits: list[TextEdit]
    cursor: CursorDescription

@dataclass
class CursorState:
    cursor: CursorDescription
    at_document_version: int
    pending_update: CursorUpdate|None

def compute_updated_cursor_position(initial_pos: Position, have_cursor_char: bool, expect_cursor_char_repair: bool,
                             change_transaction: TextDocumentContentChangeTransaction, position_codec: PositionCodec,
                             ls: LanguageServer) -> Position:
    """
        Given initial conditions and a list of document changes, compute the updated cursor position.
    """
    pos = copy.copy(initial_pos) # new position, progressively updated with each change
    #ls.show_message_log(f"compute_updated_cursor_position: {inital_pos=}", msg_type=MessageType.Log)

    for change_details in change_transaction.content_changes:
        change = change_details.minimal_change_event # Type1 change event
        assert change is not None
        range_ = change.range
        text = change.text

        if range_.end.line < pos.line:
            # here change is entirely before pos, and ends on an earlier line
            #ls.show_message_log(f"change is on line(s) prior to pos: ", msg_type=MessageType.Log)

            old_line_count = range_.end.line - range_.start.line
            new_line_count = text.count("\n")
            line_delta = new_line_count - old_line_count
            pos.line += line_delta
            # pos character is unchanged

        elif range_.end.line == pos.line and range_.end.character <= pos.character:
            # here change is entirely before pos, but ends on same line
            #ls.show_message_log(f"change is prior to pos, change ends on same line ", msg_type=MessageType.Log)

            if expect_cursor_char_repair and range_.end.character == pos.character and text.count(CURSOR_CHAR) == 1:
                # special case, we're expecting cursor repair, and the change appears to insert the cursor char
                # NOTE: we could go even more fine grained and check whether change.text ends with CURSOR_CHAR or CURSOR_CHAR+"\n"
                # that would handle changes that are more likely to be cursor repair (less false positives),
                # but be less robust for multiple merged changes which is the purpose of expect_cursor_char_repair.

                text_upto_cursor = text[0:text.index(CURSOR_CHAR)] # NB: due to if above, can assume text contains CURSOR_CHAR
                if "\n" in text_upto_cursor:
                    pos.line = range_.start.line + text_upto_cursor.count("\n")
                    # REVIEW: can avoid extra allocation using rfind instead of rsplit, see elsewhere in file
                    pos.character = position_codec.client_num_units(text_upto_cursor.rsplit("\n", 1)[-1])
                else:
                    pos.line = range_.start.line
                    pos.character = range_.start.character + position_codec.client_num_units(text_upto_cursor)

                have_cursor_char = True
                expect_cursor_char_repair = False
            else:
                old_line_count = range_.end.line - range_.start.line
                new_line_count = text.count("\n")
                line_delta = new_line_count - old_line_count
                pos.line += line_delta

                if "\n" in text:
                    # REVIEW: can avoid extra allocation using rfind instead of rsplit, see elsewhere in file
                    pos.character = position_codec.client_num_units(text.rsplit("\n", 1)[-1]) + (pos.character - range_.end.character)
                else:
                    pos.character = range_.start.character + position_codec.client_num_units(text) + (pos.character - range_.end.character)

        else:
            # here range_.end is strictly after pos
            if (range_.start.line < pos.line or
                    (range_.start.line == pos.line and range_.start.character < pos.character)):
                # here range_ overlaps insertion point.
                #ls.show_message_log(f"change overlaps pos", msg_type=MessageType.Log)

                # in most cases, a change the overlaps the insertion point can only be handled heuristically
                # so we apply a series of heuristics...

                # TODO: implement heuristic behaviors:

                did_retain_cursor_char = False

                if len(text) == 0:
                    # if change is a deletion (target text is empty)
                    # then map pos to start of deletion (which is also end of deletion)
                    pos = copy.copy(range_.start)
                    did_retain_cursor_char = False

                # TODO if we have an applicable cursor_update, attempt to match it by range and content
                # zero it out if we match it
                # even if we don't match it, zero it out at end of iteration so we don't attempt to match it later


                if have_cursor_char and text.count(CURSOR_CHAR) == 1:
                    # if have_cursor_char and to-text contains a single cursor char
                    # set pos to position of cursor char in target
                    # NOTE: this heuristic will not necessarily preserve user intent if the user
                    # pastes in some text that contains cursor_char
                    prefix = text.split(CURSOR_CHAR, 1)[0]
                    line_count = prefix.count("\n")
                    if line_count == 0:
                        pos.character = range_.start.character + position_codec.client_num_units(prefix)
                    else:
                        pos.line += line_count
                        pos.character = position_codec.client_num_units(prefix.rsplit("\n", 1)[-1])
                    did_retain_cursor_char = True

                if expect_cursor_char_repair and text.count(CURSOR_CHAR) == 1:
                    expect_cursor_char_repair = False
                    # TODO if we're expecting cursor repair and we don't currently have the cursor
                    # and the new text contains a single cursor char, set the cursor pos to the position
                    # of that char.
                    # this is more-or-less the same code as the previous heuristic

                # - COULD DO attempt to map the insertion point by using a diff algorithm
                # to compute a set of fine-grained deltas. there are a variety of user edit
                # types that we would like to accomodate, such as:
                #   - small (single char) changes on either side of the insertion pos
                #     (e.g. spelling changes, punctuation, captitalization changes)
                #   - "annular edits" that leave a significant unique island of unchanged
                #     text around the insertion pos
                # consider histogram diff or patience diff over traditional diff algorithms
                # cursor chars should be treated as anchors
                # if there are multiple active insertion points should take account of all of them

                else:
                    # REVIEW: fallback: there are four sane fallback options. set pos to:
                    #  - start of inserted range
                    #  - end of inserted range
                    #  - end of file
                    #  - halt
                    # *start of range* makes sense if the intention of the change is to add stuff after
                    # the insertion. i.e. we want to continue inserting at the end of the undamaged
                    # part of the insertion.
                    # *end of range* makes sens if the user intention is to edit
                    # the existing insertion and have the insertion continue on after the edit.
                    # *end of file* is more like a fail-safe, it continues to insert the new text in a
                    # location that is most likely to maintain coherence of future insertions, and
                    # least likely to interfere with the user's edits, but it is also a non-local jump
                    # and will is least likely to appear "natural".
                    # *halt* this will avoid corrupting the user's text. the policy decision of whether
                    # to halt or continue at the best inferred insertion pos needs to be made at a higher
                    # level.

                    # start of inserted range:
                    pos = copy.copy(range_.start)

                    # end of inserted range:
                    # pos.line = range_.start.line + text.count("\n")
                    # if "\n" in text:
                    #     pos.character = position_codec.client_num_units(text.rsplit("\n", 1)[-1])
                    # else:
                    #     pos.character = range_.start.character + position_codec.client_num_units(text)

                # update have_cursor_char after processing change change so we don't try to track it
                if have_cursor_char and not did_retain_cursor_char:
                    have_cursor_char = False
            else:
                # here, range_.start is at or after pos and range_.end is strictly after pos

                pass # change is at or after pos, ignore
                # (but note that this may alter the cursor_char or eol)

                # FIXME TODO update have_cursor_char after processing change change so we don't
                # think we are tracking it when we have lost sync.

                # COULD DO: [A]
                #   if have_cursor_char:
                #       have_cursor_char = ... # TODO set to False if the edit removed the cursor
                #   elif expect_cursor_char_repair:
                #       have_cursor_char = ... # TODO set to True if the edit inserts the cursor at pos

        #ls.show_message_log(f"compute_updated_cursor_position: {pos=}", msg_type=MessageType.Log)

    return pos

def change_startswith_edits(change: TextDocumentContentChangeTransaction, text_edits: list[TextEdit]) -> bool:
    """returns True if the change transaction begins with Type1 change events that are directly
    equivalent to the supplied list of TextEdits"""

    if len(change.content_changes) < len(text_edits):
        return False

    for i, edit in enumerate(text_edits, start=0):
        content_change = change.content_changes[i]

        if not isinstance(content_change.original_change_event, TextDocumentContentChangeEvent_Type1):
            return False

        if (content_change.original_change_event.range != edit.range or
                content_change.original_change_event.text != edit.new_text):
            return False

    return True

class QueueSentinel(Enum):
    END_OF_STREAM = 0
    REQUEST_CURSOR_REPAIR = 1

class TextInserter(TextDocumentObserver):
    def __init__(self, ls, document: TextDocument, insertion_pos: Position, eol_sequence: str, queue: asyncio.Queue[str|QueueSentinel]):
        assert document.version is not None
        self.ls = ls
        self.document = document
        self.active_cursor_states = [
                CursorState(
                    CursorDescription(position=copy.deepcopy(insertion_pos), has_cursor_char=False, has_eol=False),
                        at_document_version=document.version,
                        pending_update=None)]
        self.queue = queue
        self.eol_sequence = eol_sequence
        self.last_known_cursor_pos = None # used only while an insertion is being applied

    def notify_document_content_change(self, change: TextDocumentContentChangeTransaction) -> None:
        """Update cursors in response to document change events from the client"""
        assert change.to_document_version == self.document.version # require that the document has already been updated to the target state

        # NOTE: update cursor states in-place for performance

        do_request_cursor_repair = False
        for cursor_state in self.active_cursor_states:
            _change = change # _change and change are not necessarily equal. allow for per-state change rewrites below

            # synchronise pending update with change transaction
            expect_cursor_char_repair = False # used for heuristic cursor_char repair
            if cursor_state.pending_update:
                if cursor_state.pending_update.from_document_version < _change.from_document_version:
                    # discard stale pending update
                    cursor_state.pending_update = None

                # match pending update by version numbers:
                elif (cursor_state.pending_update.from_document_version == _change.from_document_version and
                        cursor_state.pending_update.to_document_version == _change.to_document_version):
                    cursor_state.cursor = cursor_state.pending_update.cursor
                    cursor_state.at_document_version = _change.to_document_version
                    cursor_state.pending_update = None
                    continue

                # match pending update by edit content (when _change.to_document_version > _change.from_document_version + 1)
                elif cursor_state.pending_update.from_document_version == _change.from_document_version:
                    if change_startswith_edits(_change, cursor_state.pending_update.text_edits):
                        edit_count = len(cursor_state.pending_update.text_edits)
                        cursor_state.cursor = cursor_state.pending_update.cursor
                        cursor_state.at_document_version = cursor_state.pending_update.to_document_version
                        cursor_state.pending_update = None

                        if len(_change.content_changes) == edit_count:
                            # we have matched the whole transaction
                            continue
                        else:
                            # we have matched the initial sequence of the change, still need to process the rest of the change
                            _change = copy.deepcopy(_change)
                            _change.content_changes = _change.content_changes[edit_count:]
                    else:
                        # according to the version number, the pending update is included in the
                        # change transaction, but the pending update cannot be found in the transaction by
                        # direct matching. this indicates that the change transaction includes merged, reformatted
                        # and/or re-ordered changes.
                        # since we don't know whether this situation occurs in practice, log a warning
                        # and perform a reasonable-effort recovery.

                        self.ls.show_message_log("cursor may desynchronise. change transaction contains merged client and server changes.", msg_type=MessageType.Warning)

                        # reasonable-effort recovery works as follows: if the server update does not perform
                        # cursor repair (i.e. inserts the cursor char after the insertion pos)
                        # then the insertion can be safely treated like a client insertion.
                        # but if the cursor char does perform cursor repair, then we need to identify
                        # the change that repairs the cursor_char and adjust the insertion pos accordingly.
                        if (not cursor_state.cursor.has_cursor_char
                                and cursor_state.pending_update.cursor.has_cursor_char):
                            expect_cursor_char_repair = True

                        # NOTE: a best-effort recovery would work harder to disentangle changes arising from pending_update from
                        # client changes that are included in the transaction. but since we don't know whether this can happen
                        # in practice, we perform a reasonable effort instead.
                        cursor_state.pending_update = None

                elif (cursor_state.pending_update.from_document_version >= _change.from_document_version and
                        cursor_state.pending_update.to_document_version <= _change.to_document_version):
                    # the pending update is spanned by the transaction, but not at the start.
                    # this should never happen, because pending updates are enqueued from a known document
                    # version that has already stabilised on the server.

                    self.ls.show_message_log("unexpected state: change transaction spans over server change. cursor may desynchronise.", msg_type=MessageType.Error)

                    cursor_state.pending_update = None
                else:
                    # the pending update applies to a future transaction. nothing to do for now.
                    pass

            new_cursor = CursorDescription(
                    position=compute_updated_cursor_position(
                        initial_pos=cursor_state.cursor.position,
                        have_cursor_char=cursor_state.cursor.has_cursor_char,
                        expect_cursor_char_repair=expect_cursor_char_repair,
                        change_transaction=_change,
                        position_codec=self.document.position_codec, ls=self.ls),
                    has_cursor_char=False, has_eol=False)

            # brute-force compute new_cursor.has_cursor_char and new_cursor.has_eol from new document state
            lines = self.document.lines
            py_pos = self.document.position_codec.position_from_client_units(lines, new_cursor.position)
            if py_pos.line < len(lines):
                line = lines[py_pos.line]
                if line.startswith(CURSOR_CHAR, py_pos.character):
                    new_cursor.has_cursor_char = True
                    if line.startswith("\n", py_pos.character+1) or line.startswith("\r\n", py_pos.character+1):
                        new_cursor.has_eol = True

            do_request_cursor_repair = do_request_cursor_repair or (cursor_state.cursor.has_cursor_char and new_cursor.has_cursor_char)
            cursor_state.cursor = new_cursor
            cursor_state.at_document_version = _change.to_document_version

        if do_request_cursor_repair:
            self.queue.put_nowait(QueueSentinel.REQUEST_CURSOR_REPAIR)

    def try_begin_edit(self) -> CursorState|None: # returns a new CursorState upon success
        assert len(self.active_cursor_states) == 1
        # the only time there should be multiple cursors is during the workspace edit in try_apply_text_edits
        # and this function should not be called concurrently to that on the same document.
        if self.active_cursor_states[0].pending_update:
            # don't insert more text until the document changes from the previous insertion
            # have been processed, and the cursor updated accordingly
            return None

        # snapshot current cursor state to avoid concurrency bugs, since update_insertion_pos works in-place
        cursor_state = CursorState(
                cursor=copy.deepcopy(self.active_cursor_states[0].cursor),
                at_document_version=self.active_cursor_states[0].at_document_version,
                pending_update=None
            )

        assert cursor_state.at_document_version == self.document.version
        assert not cursor_state.pending_update
        return cursor_state

    async def try_apply_edits(self, text_edits: list[TextEdit], cursor_state: CursorState, cursor_upon_success: CursorDescription):
        # use versioned document changes API:
        # this ensures that the edit is only applied to the expected
        # version of the document, otherwise return False and retry
        text_document_edit = TextDocumentEdit(
            text_document=OptionalVersionedTextDocumentIdentifier(
                uri=self.document.uri,
                version=cursor_state.at_document_version),
            edits=text_edits) # type error: assigning list[TextEdit] to list[TextEdit | AnnotatedTextEdit]
        workspace_edit = WorkspaceEdit(document_changes=[text_document_edit])
        label = "Prapti: Insert Text" # UI edit description, e.g. for undo, not visible in VSCode on Windows

        # ---

        # while the edit is being applied there are two possible futures:
        # (a) the edit is applied sucessfully
        # (b) something happens (such as a race with a user edit) and the edit
        # fails. we use two cursors to track the outcome in both cases, and then
        # disable the unsuccessful case once we know whether the edit succeeds.
        # this mechanism is robust to whether we receive a document change notification
        # either before or after we receive acknowledgement of the workspace edit here.

        assert len(self.active_cursor_states) == 1
        self.last_known_cursor_pos = copy.copy(self.active_cursor_states[0].cursor.position)
        failure_path = self.active_cursor_states[0] # continue tracking cursor as if this insertion never happened
        success_path = CursorState(
            cursor=copy.deepcopy(cursor_state.cursor),
            at_document_version=cursor_state.at_document_version,
            pending_update=CursorUpdate(
                    from_document_version=cursor_state.at_document_version,
                    to_document_version=cursor_state.at_document_version + 1,
                    text_edits=text_edits,
                    cursor=cursor_upon_success
                )
            )
        self.active_cursor_states = [success_path, failure_path]

        # commit the edit
        result = await self.ls.apply_edit_async(edit=workspace_edit, label=label)

        # old:
        # go low-level because pygls doesn't currently have an async apply edit function
        # see: https://github.com/openlawlibrary/pygls/pull/350/commits/aa9251a4d0849afc36bfcdc3565eb4fa9b094f3b
        # result = await self.ls.lsp.send_request_async(WORKSPACE_APPLY_EDIT,
        #     ApplyWorkspaceEditParams(edit=workspace_edit, label=label))

        # NOTE: document version and insertion pos will be updated by update_insertion_pos
        # when our server workspace copy of the document gets synchronised

        # collapse to the correct path, now that we know the edit outcome
        # note that this works whether or not the pending update has already been processed asynchronously
        if result.applied:
            self.active_cursor_states = [success_path]
        else:
            self.active_cursor_states = [failure_path]

        refresh_code_lens = False
        if self.active_cursor_states[0].cursor.position.line != self.last_known_cursor_pos.line:
            # notify workspace to force updating the Run/Stop Prapti code lens location
            refresh_code_lens = True
        self.last_known_cursor_pos = None

        if refresh_code_lens: # do this after we've cleared self.last_known_cursor_pos:
            pass
            #await request_refresh_code_lens(self.ls) # REVIEW either strip out code_lens support or implement it

        return result.applied

    async def try_insert_text(self, text) -> bool:
        """Try to insert `text` into the document, at the same time repair the cursor if necessary.
        `text` may be empty, which will cause cursor repair to happen on its own.
        Return True if the edit succeeded.
        """

        cursor_state = self.try_begin_edit()
        if not cursor_state:
            return False

        # --- compute the text edit and the new cursor

        cursor = cursor_state.cursor
        insertion_pos = cursor.position
        insertion_range = Range(start=insertion_pos, end=insertion_pos)

        # compute cursor_upon_success: the hypothetical new cursor if the update succeeds
        new_position = Position(line=cursor.position.line, character=cursor.position.character)
        position_codec = self.document.position_codec
        if "\n" in text: # NOTE: this will work with "\r\n" EOL sequences too
            new_position.line += text.count("\n")
            new_position.character = position_codec.client_num_units(text.rsplit("\n", 1)[-1])
        else:
            new_position.character += position_codec.client_num_units(text)
        cursor_upon_success = CursorDescription(position=new_position, has_cursor_char=cursor.has_cursor_char, has_eol=cursor.has_eol)

        cursor_repair_text = ""
        if not cursor.has_cursor_char:
            cursor_repair_text += CURSOR_CHAR
            cursor_upon_success.has_cursor_char = True
            # note, in a single edit we can only append a newline if we've already appended the cursor char
            # if we want to maintain the newline always, we could do multiple edits
            if not cursor.has_eol:
                cursor_repair_text += self.eol_sequence # REVIEW: not sure we should repair missing newline except at start, maybe not for @prompt completions
                cursor_upon_success.has_eol = True
        new_text = text + cursor_repair_text

        if not new_text:
            # no edit is required. there is no text to insert and no cursor repair is necessary
            return True

        # REVIEW: as of lsp 3.16 we could maybe use AnnotatedTextEdit here to group changes
        text_edits = [TextEdit(range=insertion_range, new_text=new_text)]

        # ---

        return await self.try_apply_edits(text_edits, cursor_state, cursor_upon_success)

    async def try_remove_cursor_sequence(self) -> bool:
        cursor_state = self.try_begin_edit()
        if not cursor_state:
            return False

        cursor = cursor_state.cursor

        # compute deletion range
        start = copy.copy(cursor.position)
        if cursor.has_cursor_char and cursor.has_eol:
            end = Position(start.line + 1, 0)
        elif cursor.has_cursor_char:
            end = copy.copy(cursor.position)
            end.character += self.document.position_codec.client_num_units(CURSOR_CHAR)
        else:
            # nothing to remove
            return True
        deletion_range = Range(start=start, end=end)

        text_edits = [TextEdit(range=deletion_range, new_text="")]

        cursor_upon_success = CursorDescription(position=cursor.position, has_cursor_char=False, has_eol=False)

        return await self.try_apply_edits(text_edits, cursor_state, cursor_upon_success)

    def get_cursor_pos(self):
        """Retrieve the best estimate of the cursor position. may be called asynchronously to insertions"""
        if len(self.active_cursor_states) == 1:
            return self.active_cursor_states[0].cursor.position
        assert self.last_known_cursor_pos is not None
        return self.last_known_cursor_pos

async def enqueue_text_from_source(source: AsyncGenerator[str, None], queue: asyncio.Queue[str|QueueSentinel]):
    try:
        async for item in source:
            queue.put_nowait(item)
    except asyncio.CancelledError:
        pass
    finally:
        queue.put_nowait(QueueSentinel.END_OF_STREAM)

async def insert_queued_text(queue: asyncio.Queue[str|QueueSentinel], inserter: TextInserter):
    """process that dequeues text and events from the queue and buffers them locally
    until they are sucessfully inserted into the text inserter.
    REQUEST_CURSOR_REPAIR causes try_insert_text to be called even if there is no pending text"""
    pending = ""
    repair_cursor = True # repair cursor at start of run
    at_end = False
    while True:
        if not at_end:
            # copy more input into pending buffer
            try:
                while True:
                    if pending or repair_cursor:
                        # if we already have pending input, or we need to repair the cursor,
                        # accumulate more without blocking
                        s = queue.get_nowait()
                    else:
                        s = await queue.get() # block until data is available
                    if s == QueueSentinel.END_OF_STREAM:
                        at_end = True
                        queue.task_done()
                        break
                    elif s == QueueSentinel.REQUEST_CURSOR_REPAIR:
                        repair_cursor = True
                    else:
                        pending += s
                    queue.task_done()
            except asyncio.QueueEmpty: # thrown by queue.get_nowait()
                pass

        if at_end and not pending:
            break # done

        edit_succeeded = await inserter.try_insert_text(pending)
        if edit_succeeded:
            pending = ""
            repair_cursor = False
        else:
            await asyncio.sleep(0.1) # rate limit retries

    # NOTE: the queue may not be empty at this point, because additional
    # REQUEST_CURSOR_REPAIR events may be pushed. we just ignore them

    # remove insertion cursor sequence from document
    while True:
        edit_succeeded = await inserter.try_remove_cursor_sequence()
        if edit_succeeded:
            break
        else:
            await asyncio.sleep(0.1) # rate-limit retries
