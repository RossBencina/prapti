import asyncio

from pygls.server import LanguageServer
from lsprotocol.types import (
    MessageType,
    Command,
    TEXT_DOCUMENT_CODE_ACTION,
    CodeAction,
    CodeActionKind,
    CodeActionOptions,
    CodeActionParams
)

from .observable_lsp import ObservableLanguageServerProtocol
from .prapti_run import LsPraptiRun

class PraptiLanguageServer(LanguageServer):
    CMD_RUN_PRAPTI = "runPrapti"
    CMD_STOP_PRAPTI = "stopPrapti"

    CONFIGURATION_SECTION = "praptiLanguageServer"

    def __init__(self, *args):
        self._active_prapti_runs: dict[str, 'LsPraptiRun'] = {}
        super().__init__(protocol_cls=ObservableLanguageServerProtocol, *args)

    async def run_prapti(self, document_uri: str):
        if document_uri in self._active_prapti_runs: # limit to one active run per document
            self.show_message_log(f"Prapti is already running on document {document_uri}", msg_type=MessageType.Log)
            return

        document = self.workspace.get_text_document(document_uri)
        try:
            prapti_run = LsPraptiRun()
            self._active_prapti_runs[document.uri] = prapti_run
            await prapti_run.run(self, self.lsp, document)
        finally:
            del self._active_prapti_runs[document.uri]

    async def stop_prapti(self, document_uri: str):
        if prapti_run := self._active_prapti_runs.get(document_uri, None):
            await prapti_run.cancel()

server = PraptiLanguageServer("prapti-language-server", "v0.1") # FIXME review params

@server.command(PraptiLanguageServer.CMD_RUN_PRAPTI)
async def run_prapti(ls: PraptiLanguageServer, *args):
    ls.show_message_log(f"Prapti language server command: {PraptiLanguageServer.CMD_RUN_PRAPTI}", msg_type=MessageType.Log)
    arguments = args[0]
    document_uri = arguments[0]
    asyncio.create_task(ls.run_prapti(document_uri))

@server.command(PraptiLanguageServer.CMD_STOP_PRAPTI)
async def stop_prapti(ls: PraptiLanguageServer, *args):
    ls.show_message_log(f"Prapti language server command: {PraptiLanguageServer.CMD_STOP_PRAPTI}", msg_type=MessageType.Log)
    arguments = args[0]
    document_uri = arguments[0]
    await ls.stop_prapti(document_uri)

@server.feature(
    TEXT_DOCUMENT_CODE_ACTION,
    CodeActionOptions(code_action_kinds=[CodeActionKind.Source]),
)
def code_actions(params: CodeActionParams):
    document_uri = params.text_document.uri
    return [
        CodeAction(
            title="Run Prapti",
            kind="source.prapti.run",
            command=Command(
                title="Run Prapti",
                command=PraptiLanguageServer.CMD_RUN_PRAPTI,
                arguments=[document_uri]
            ),
        ),
        CodeAction(
            title="Stop Prapti",
            kind="source.prapti.stop",
            command=Command(
                title="Stop Prapti",
                command=PraptiLanguageServer.CMD_STOP_PRAPTI,
                arguments=[document_uri]
            ),
        ),
    ]

def main() -> int:
    server.start_io()
    return 0
