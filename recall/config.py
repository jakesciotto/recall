"""Configuration. Everything has a working default, so a first run needs no
edits at all. Override any of it through the environment or a .env file.
"""

import os
import pathlib

def _env(name, default):
    return os.environ.get(name, default)


DATA_DIR = pathlib.Path(_env("RECALL_DATA", "./data")).expanduser()
WORK_DIR = pathlib.Path(_env("RECALL_WORK", "./work")).expanduser()

# The embedding and answer endpoints speak the OpenAI API, so any server that
# does will work: llama.cpp, llama-swap, Ollama, vLLM, LM Studio.
EMBED_URL = _env("RECALL_EMBED_URL", "http://127.0.0.1:8080/v1/embeddings")
EMBED_MODEL = _env("RECALL_EMBED_MODEL", "embeddings")
EMBED_DIMS = int(_env("RECALL_EMBED_DIMS", "1024"))

# Optional: only needed to write prose answers. Indexing and search do
# not use it. Point this at any OpenAI-compatible endpoint you run.
CHAT_URL = _env("RECALL_CHAT_URL", "")
CHAT_MODEL = _env("RECALL_CHAT_MODEL", "")
# The corpus labels the user's own lines "me:". Set this to the user's name
# and the prompt relabels those lines; storage never changes. See
# docs/answering.md.
USER_LABEL = _env("RECALL_USER_LABEL", "")
# The model that grades logged answers. A model grading its own output
# shows self-preference bias, so name a different one. See docs/evaluating.md.
JUDGE_MODEL = _env("RECALL_JUDGE_MODEL", "")

TOKENIZE_URL = _env("RECALL_TOKENIZE_URL", "")

# The server's -ub / batch size. A single request must fit inside it.
EMBED_CONTEXT = int(_env("RECALL_EMBED_CONTEXT", "8192"))

PG_DSN = _env("RECALL_PG_DSN", "postgresql://recall:recall@127.0.0.1:5433/recall")

API_TOKEN = _env("RECALL_API_TOKEN", "")
API_BIND = _env("RECALL_API_BIND", "127.0.0.1")
API_PORT = int(_env("RECALL_API_PORT", "8099"))
