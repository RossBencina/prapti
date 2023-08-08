"""
    David Shapiro memory assistant responder

    Based on https://github.com/daveshap/ChromaDB_Chatbot_Public
"""
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ..core.plugin import Plugin, PluginCapabilities, PluginContext
from ..core.command_message import Message
from ..core.configuration import VarRef, resolve_var_refs
from ..core.responder import Responder, ResponderContext
from ..core.builtins import delegate_generate_responses

@dataclass
class KnowledgeBaseArticle:
    id: str|None
    content: str

class TestResponderConfiguration(BaseModel):
    """Configuration parameters for test responder."""
    model_config = ConfigDict(
        validate_assignment=True)

    # expects a git clone of https://github.com/daveshap/ChromaDB_Chatbot_Public here, for the file system layout and prompts
    memory_system_root: str = "C:/Users/Ross/Desktop/prapti-dev/ross_prapti/ChromaDB_Chatbot_Public"

    source_responder_name: str = "default" # the responder that the user is conducting a conversation with

    memory_maintenance_responder_name: str = "default" # the responder that performs profile and kb article update. use temperature=0


class MemoryAssistantResponder(Responder):
    def construct_configuration(self, context: ResponderContext) -> BaseModel|tuple[BaseModel, list[tuple[str,VarRef]]]|None:
        return TestResponderConfiguration(), []

    def fetch_relevant_kb_article(self, conversation: list[Message], config: TestResponderConfiguration, context: ResponderContext) -> KnowledgeBaseArticle:
        """Given the current user/assistant conversation, fetch KB article from chromadb
           Use the most recent 5 user+assistant messages as query text"""

        kb_id = "63ffd04a-a08a-4884-a619-49c213943429" # HACK always return the same kb article for now. later: query chroma for best match
        kb_path = self.memory_system_root / "chromadb" / f"{kb_id}.txt"
        if kb_path.exists():
            return KnowledgeBaseArticle(id=kb_id, content=kb_path.read_text(encoding="utf-8"))
        else:
            return KnowledgeBaseArticle(id=None, content="No relevant KB article available.")

    def save_kb_article(self, kb: KnowledgeBaseArticle, config: TestResponderConfiguration, context: ResponderContext):
        #kb_id = kb.id if kb.id else str(uuid4())
        kb_id = kb.id if kb.id else "63ffd04a-a08a-4884-a619-49c213943429"
        (self.memory_system_root / "chromadb" / f"{kb_id}.txt").write_text(kb.content, encoding="utf-8")

    def generate_responses(self, input_: list[Message], context: ResponderContext) -> list[Message]:
        config: TestResponderConfiguration = context.responder_config
        context.log.debug(f"prapti.experimental.memory_assistant: input: {config = }", context.state.input_file_path)
        config = resolve_var_refs(config, context.root_config, context.log)
        context.log.debug(f"prapti.experimental.memory_assistant: resolved: {config = }", context.state.input_file_path)

        self.memory_system_root = Path(config.memory_system_root)
        user_profile_path = self.memory_system_root / "user_profile.txt"
        current_profile = user_profile_path.read_text(encoding="utf-8")
        KB = self.fetch_relevant_kb_article(input_, config, context)

        # construct system prompt with injected KB article and current user profile
        system_prompt_template = (self.memory_system_root / "system_default.txt").read_text(encoding="utf-8")
        system_prompt_str = system_prompt_template.replace("<<PROFILE>>", current_profile).replace("<<KB>>", KB.content)

        # generate conversation response
        system_message = Message(role="system", name=None, content=[system_prompt_str])
        input_with_injected_system_prompt = [system_message] + input_
        context.log.debug("input_with_injected_system_prompt = ", str(input_with_injected_system_prompt))
        responses = delegate_generate_responses(context.state, config.source_responder_name, input_with_injected_system_prompt)

        # update user profile using most recent 3 user messages
        user_scratchpad = "\n".join(["".join(message.content).strip() for message in input_ if message.role == "user" and message.is_enabled][-3:])
        profile_length = len(current_profile.split(' '))
        profile_conversation = [
            Message(role="system", name=None, content=[(self.memory_system_root / "system_update_user_profile.txt").read_text(encoding="utf-8").replace('<<UPD>>', current_profile).replace('<<WORDS>>', str(profile_length))]),
            Message(role="user", name=None, content=[user_scratchpad])
        ]
        context.log.debug("profile_conversation = ", str(profile_conversation))
        profile_responses = delegate_generate_responses(context.state, config.memory_maintenance_responder_name, profile_conversation)
        if not context.root_config.prapti.dry_run:
            user_profile_path.write_text(profile_responses[0].content[0], encoding="utf-8")

        # update knowledge base
        conversation_including_responses = input_ + responses
        # most recent 5 messages, as a string
        scratchpad = "\n".join([ f"{message.role.upper()}: {''.join(message.content).strip()}" for message in input_ if message.role in ("user", "assistant") and message.is_enabled][-5:])
        KB = self.fetch_relevant_kb_article(conversation_including_responses, config, context)
        if KB.id is None:
            # derive a new kb article from most recent 5 messages, and add article to db
            # create a new kb: /ChromaDB_Chatbot_Public/system_instantiate_new_kb.txt
            new_kb_conversation = [
                Message(role="system", name=None, content=[(self.memory_system_root / "system_instantiate_new_kb.txt").read_text(encoding="utf-8")]),
                Message(role="user", name=None, content=[scratchpad])
            ]
            context.log.debug("new_kb_conversation = ", str(new_kb_conversation))
            new_kb_responses = delegate_generate_responses(context.state, config.memory_maintenance_responder_name, new_kb_conversation)
            if not context.root_config.prapti.dry_run:
                self.save_kb_article(KnowledgeBaseArticle(id=None, content=new_kb_responses[0].content[0]), config, context)
        else:
            # update existing KB and store in db using same id from most recent 5 messages
            # update an existing kb: /ChromaDB_Chatbot_Public/system_update_existing_kb.txt
            update_kb_conversation = [
                Message(role="system", name=None, content=[(self.memory_system_root / "system_update_existing_kb.txt").read_text(encoding="utf-8").replace('<<KB>>', KB.content)]),
                Message(role="user", name=None, content=[scratchpad])
            ]
            context.log.debug("update_kb_conversation = ", str(update_kb_conversation))
            update_kb_responses = delegate_generate_responses(context.state, config.memory_maintenance_responder_name, update_kb_conversation)
            if not context.root_config.prapti.dry_run:
                self.save_kb_article(KnowledgeBaseArticle(id=None, content=update_kb_responses[0].content[0]), config, context)

            # TODO: split KB if > 1000 words
            # store first split in KB.id, second split in new KB
            # split kb: /ChromaDB_Chatbot_Public/system_split_kb.txt
            pass

        return responses

class MemoryAssistantResponderPlugin(Plugin):
    def __init__(self):
        super().__init__(
            api_version = "0.1.0",
            name = "prapti.experimental.memory_assistant",
            version = "0.0.1",
            description = "Memory assistant responder",
            capabilities = PluginCapabilities.RESPONDER
        )

    def construct_responder(self, context: PluginContext) -> Responder|None:
        return MemoryAssistantResponder()

prapti_plugin = MemoryAssistantResponderPlugin()
