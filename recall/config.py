"""Configuration. Everything has a working default, so a first run needs no
edits at all. Override any of it through the environment or a .env file.
"""

import os
import pathlib
import re

_LINE = re.compile(r"""^\s*(?:export\s+)?(RECALL_[A-Z0-9_]+)\s*=\s*(.*?)\s*$""")


def load_dotenv(path=None):
    """Read RECALL_* lines from .env into the environment, never overriding
    a variable the shell already set.

    The README says cp .env.example .env. docker compose reads that file for
    the containers, and the CLI has to read it too, or a chat URL set there
    reports as unset. Only RECALL_ keys are taken: the same file can carry
    POSTGRES_PASSWORD for compose, and that is not the CLI's business.
    """
    path = pathlib.Path(path or ".env")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        m = _LINE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if value[:1] in ("'", '"') and value[-1:] == value[:1] and len(value) > 1:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        os.environ.setdefault(key, value)


load_dotenv()


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
