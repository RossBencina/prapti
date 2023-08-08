David Shapiro memory assistant responder
Based on https://github.com/daveshap/ChromaDB_Chatbot_Public

Main file: prapti\plugins\memory_assistant_responder.py
---

Things we'll need to implement:

- given prapti message list, fetch KB article from chromadb
    (use most recent 5 user+assistant messages to generate key)

- inject data into main prapti message list, specifically system_default.txt with <<PROFILE>> and <<KB>>
    (PROFILE is read from text file)

- [*] generate conversation response using downstream responder (default responder) + add response to "most recent messages"

- [*] update user profile using 3 most recent *user* messages

- if chromadb is empty:
    - [*] derive a new kb article from most recent 5 messages, store in chroma
- else:
    - using the KB article fetched above, update it in two steps:
        1. [*] expand current KB article with new information from most recent 5 messages (including latest response), then
        2. [*] if current KB > 1000 words, split KB into two KBs
        then store the outcome of these steps in chromadb
---

Steps marked with [*] entail LLM generation


For initial version:

- use hardcoded file paths

==========

We ran into some issues with Chroma DB having a dependency on pydantic 1.10 while Prapti has a 2.1 dependency.  There is an issue on their github that is relatively recent but it doesn't look like a high priority for them: https://github.com/chroma-core/chroma/issues/893

We did some pretty light searching to see if there was an easy vector DB to drop in locally instead but nothing showed up.  We're looking at Facebook's FAISS (https://github.com/facebookresearch/faiss) although that isn't apparently an actual Vector DB but a library for effecient similarity search and clustering (according to this: https://harishgarg.com/writing/best-vector-databases-for-ai-apps/).  There is also an experimental embedded version of Weaviate that might work for us (https://weaviate.io/developers/weaviate/installation/embedded)

---

We started to discuss issues with DS's process and how it is very particular to the kb domain and whether that is a relevant model for Prapti.  Also about some of the assumptions it makes (or at least seams to make) that you're working with a large dataset.

---

API needs improvement if we're going to use it in this manner.  We need a flat file of the responses too.

Log all updates to db_logs/ like in Dave's script

---

Idea: make the names of the source assistant responder and the kb maintenance responder settable using attributes in the config.