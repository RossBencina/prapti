import os
from typing import Callable

# determine document-consistent line endings when inserting text into document

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

# FIXME: as of 10/10/23 prapti.tool doesn't respect the file's line ending style, it just reads/writes '\n' line endings and
# expects text-mode file io to translate.

def detect_eol_sequence(source: str):
    """Returns the end of line sequence based on the document source text or native line ending sequence mode"""
    if "\r\n" in source:
        return "\r\n"
    elif "\n" in source:
        return "\n"
    else:
        # HACK fallback if document contains no end of line characters (e.g. empty document)
        return os.linesep
        # REVIEW: could maybe use "\n" always if the client performs normalization (? see notes above)
        # REVIEW: could/should consult .editorconfig instead of using os.linesep default

def rewrite_line_endings_to_lf(s: str) -> str:
    if "\r\n" in s:
        return s.replace("\r\n", "\n")
    return s

def rewrite_line_endings_to_crlf(s: str) -> str:
    if "\n" in s:
        if "\r\n" in s:
            return s.replace("\r\n", "\n").replace("\n", "\r\n") # idempotent in the presence of "\r\n"
        else:
            return s.replace("\n", "\r\n")
    else:
        return s

def select_line_endings_rewriter(eol_sequence: str) -> Callable[[str], str]:
    return rewrite_line_endings_to_lf if eol_sequence == "\n" else rewrite_line_endings_to_crlf
