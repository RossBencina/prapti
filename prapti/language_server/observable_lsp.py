import abc
import copy
from dataclasses import dataclass

from pygls.protocol import LanguageServerProtocol, lsp_method
from pygls.workspace import TextDocument, PositionCodec
from lsprotocol.types import (
    MessageType,
    Position,
    Range,
    TEXT_DOCUMENT_DID_CHANGE,
    DidChangeTextDocumentParams,
    TextDocumentContentChangeEvent_Type1,
    TextDocumentContentChangeEvent_Type2
)

def client_pos_from_index(s: str, index: int, position_codec: PositionCodec) -> Position:
    line_start = s.rfind("\n", 0, index) + 1 # index of start of line containing index, or start of string if "\n" is not found
    # ^^^ note, rfind returns -1 if not found, which is exactly what we want with adding +1
    return Position(line=s.count("\n", 0, index), character=position_codec.client_num_units(s[line_start:index]))

# extended wrappers from LSP events:

@dataclass
class TextDocumentContentChangeDetails: # TODO rename TextDocumentContentChangeEventDetails
    """Wrapper for lsprotocol.TextDocumentContentChangeEvent_Type{1,2} that stores additional
    computed information associated with the change."""
    original_change_event: TextDocumentContentChangeEvent_Type1|TextDocumentContentChangeEvent_Type2
    from_text: str
    to_text: str
    minimal_change_event: TextDocumentContentChangeEvent_Type1|None = None

@dataclass
class TextDocumentContentChangeTransaction:
    """Alternative representation of DidChangeTextDocumentParams that includes additional
    details of each change using TextDocumentContentChangeDetails
    """
    document: TextDocument
    from_document_version: int
    to_document_version: int
    content_changes: list[TextDocumentContentChangeDetails]

# observer. implement this interface

class TextDocumentObserver(metaclass=abc.ABCMeta):
    @abc.abstractmethod
    def notify_document_content_change(self, change: TextDocumentContentChangeTransaction) -> None:
        pass

def _minimal_contiguous_difference(a: str, b: str) -> tuple[tuple[int,int],tuple[int,int]]: # -> (astart,aend), (bstart,bend)
    len_a = len(a)
    len_b = len(b)
    if len_a == 0:
        if len_b == 0:
            return (0,0), (0,0) # no change
        return (0,0), (0, len_b) # insert entire b
    if len_b == 0:
        return (0, len_a), (0,0) # delete entire a
    if len_a < len_b:
        if b.startswith(a):
            return (len_a, len_a), (len_a, len_b) # b extends a
        elif b.endswith(a):
            return (0,0), (0, len_b - len_a) # b prefixes a
    elif len_b < len_a:
        if a.startswith(b):
            return (len_b, len_a), (len_b, len_b) # b trims end of a
        elif a.endswith(b):
            return (0, len_a - len_b), (0,0) # b trims start of a
    else: # len_a == len_b and not 0
        if a == b:
            return (0,0), (0,0) # no change

    # at this point, both a and b are non-empty, non-equal
    # and neither is a prefix or suffix of the other
    # so searching from each end will find a non-equal character

    min_len = min(len_a, len_b)

    # search forward for the first differing change
    forwards_first_differing_index = None
    for i in range(0, min_len):
        if a[i] != b[i]:
            forwards_first_differing_index = i
            break
    assert forwards_first_differing_index is not None # not possible, because such cases are handled above

    # seach backward, from ends, for the first differing change from end
    backwards_last_differing_indices = None
    for j in range(1, min_len + 1):
        if a[len_a - j] != b [len_b - j]:
            backwards_last_differing_indices = (len_a - j, len_b - j)
            break
    assert backwards_last_differing_indices is not None # not possible

    return (forwards_first_differing_index, backwards_last_differing_indices[0]+1), (forwards_first_differing_index, backwards_last_differing_indices[1]+1)

def minimal_contiguous_difference(a: str, b: str, ls) -> tuple[tuple[int,int],tuple[int,int]]: # -> (astart,aend), (bstart,bend)
    """checked version, throws an assertion if the difference fails postconditions"""

    result = _minimal_contiguous_difference(a, b)
    #print(result)
    a_pre, a_diff, a_post = a[0:result[0][0]], a[result[0][0]:result[0][1]], a[result[0][1]:]
    b_pre, b_diff, b_post = b[0:result[1][0]], b[result[1][0]:result[1][1]], b[result[1][1]:]
    #print(a_pre, a_diff, a_post)
    #print(b_pre, b_diff, b_post)
    result_ = None

    try:
        # invariant checks:
        assert a_pre == b_pre
        assert a_post == b_post
        assert a_diff != b_diff or (a_diff == "" and b_diff == "")

        # change applies correctly in both directions:
        assert a_pre + b_diff + a_post == b
        assert b_pre + a_diff + b_post == a

        # change is minimal:
        if len(a_diff) > 0 and len(b_diff) > 0:
            assert a_diff[0] != b_diff[0]
            assert a_diff[-1] != b_diff[-1]

        # symmetrical
        result_ = _minimal_contiguous_difference(b, a) # pylint: disable=arguments-out-of-order
        assert result == (result_[1],result_[0])

    except AssertionError:
        ls.show_message_log(f"{result=}, {result_=}", msg_type=MessageType.Log)
        ls.show_message_log(f"{a_pre=}, {a_diff=}, {a_post=}", msg_type=MessageType.Log)
        ls.show_message_log(f"{b_pre=}, {b_diff=}, {b_post=}", msg_type=MessageType.Log)

    return result

def compute_minimal_change_event(change_details: TextDocumentContentChangeDetails, position_codec: PositionCodec, ls):
    """Given a change event, compute its minimal contiguous change as a
    TextDocumentContentChangeEvent_Type1 event.

    This function serves two purposes:
        1. minimise changes so that they don't unnecessarily overlap the insertion point
        2. transform all Type2 changes to Type1
    """

    # short-circuit Type1 changes that are inherently minimal
    if isinstance(change_details.original_change_event, TextDocumentContentChangeEvent_Type1):
        if ( (change_details.original_change_event.range.start == change_details.original_change_event.range.end) # insertion
                or (not change_details.original_change_event.text) # deletion
                ):
            change_details.minimal_change_event = change_details.original_change_event
            return

    # contract range and text so that the change is as small as possible

    # get change data:
    # match change_details.original_change_event:
    #     case TextDocumentContentChangeEvent_Type1(range=range_, text=text):
    #         pass
    #     case TextDocumentContentChangeEvent_Type2(text=text):
    #         range_ = Range(Position(0, 0), make_end_of_file_position(text, document.position_codec))

    # we could try to use the existing change as a starting point, but for now
    # brute-force compute change region char-by-char

    diff = minimal_contiguous_difference(change_details.from_text, change_details.to_text, ls)
    diff_is_empty = (diff[0][0] == diff[0][1] and diff[1][0] == diff[1][1])
    if diff_is_empty:
        range_ = Range(Position(0,0), Position(0,0))
        text = ""
    else:
        start = client_pos_from_index(change_details.from_text, diff[0][0], position_codec)
        end = client_pos_from_index(change_details.from_text, diff[0][1], position_codec)
        range_ = Range(start, end)
        text = change_details.to_text[diff[1][0]:diff[1][1]]

    change_details.minimal_change_event = TextDocumentContentChangeEvent_Type1(range=range_, text=text)

class ObservableLanguageServerProtocol(LanguageServerProtocol):
    """Extend pygls.LanguageServerProtocol with fine-grained
    document change observation capability."""

    def __init__(self, server, converter):
        self._observers: dict[str, list] = {}
        super().__init__(server, converter)

    def register_observer(self, document_uri: str, observer: TextDocumentObserver):
        if document_uri in self._observers:
            self._observers[document_uri].append(observer)
        else:
            self._observers[document_uri] = [observer]

    def unregister_observer(self, document_uri: str, observer: TextDocumentObserver):
        if document_uri in self._observers:
            self._observers[document_uri].remove(observer)
            if not self._observers[document_uri]:
                del self._observers[document_uri]

    @lsp_method(TEXT_DOCUMENT_DID_CHANGE)
    def lsp_text_document__did_change(
        self, params: DidChangeTextDocumentParams
    ) -> None:
        """Updates document's content."""
        document_uri = params.text_document.uri
        # if not document_uri in self._observers:
        #     # bypass our change-processing algorithm if there are no observers
        #     # REVIEW: especially while debugging we may not want to do this since we
        #     # will want to test change processing on all notifications, whether or not
        #     # there are observers.
        #     super().lsp_text_document__did_change(params)
        #     return

        # Override the superclass method in order to notify observers...

        # REVIEW: The pygls docs are ambiguous about how we're supposed override built-in
        # language server methods. The comment in protocol.py::LSPMeta suggests that super
        # will be called automatically. but logging shows that it is not called.

        #super().lsp_text_document__did_change(params)
        #self.show_message_log("prapti lsp_text_document__did_change", msg_type=MessageType.Log)

        assert self.workspace
        document = self.workspace.get_text_document(document_uri)
        assert document.version is not None
        change_transaction = TextDocumentContentChangeTransaction(
            document=document,
            from_document_version=document.version,
            to_document_version=params.text_document.version,
            content_changes=[]
        )

        # this loop updates the document just as in super().lsp_text_document__did_change(params)
        # in addition it accumulates changes into change_transaction
        for change in params.content_changes:
            from_text = copy.copy(document.source)
            self.workspace.update_text_document(params.text_document, change)
            to_text = copy.copy(document.source)
            if to_text != from_text:
                change_details = TextDocumentContentChangeDetails(
                    original_change_event=change,
                    from_text=from_text,
                    to_text=to_text
                )
                compute_minimal_change_event(change_details, document.position_codec, self._server)
                assert change_details.minimal_change_event is not None

                # if minimisation actually did something, log it.
                # this happens in at least the following cases:
                #   - the language client sent us a non-minimal change
                #   - a replacement edit is made where the new text is either
                #     the prefix or suffix of the old text.
                # NOTE: it's an open question whether we should retain the full text of either of
                # these in the interests of intention preservation.
                log_minimsation_details = False
                match change_details.original_change_event:
                    case TextDocumentContentChangeEvent_Type1():
                        log_minimsation_details = log_minimsation_details or (
                            change_details.minimal_change_event.range != change_details.original_change_event.range or
                            change_details.minimal_change_event.text != change_details.original_change_event.text)
                    case TextDocumentContentChangeEvent_Type2():
                        self._server.show_message_log("converted type-2 change to type-1", msg_type=MessageType.Log)
                        log_minimsation_details = log_minimsation_details or True

                if log_minimsation_details:
                    self._server.show_message_log(f"{change_details.original_change_event = }", msg_type=MessageType.Log)
                    self._server.show_message_log(f"{change_details.minimal_change_event = }", msg_type=MessageType.Log)
                    #self._server.show_message_log(f"{change_details.from_text = }\n{change_details.to_text = }", msg_type=MessageType.Log)

                change_transaction.content_changes.append(change_details)

        # notify observers...
        if params.text_document.uri in self._observers:
            # TODO: allow observers to be added and removed during the notification loop
            for observer in self._observers[params.text_document.uri]:
                observer.notify_document_content_change(change_transaction)
