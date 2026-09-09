"""Caption an image through any OpenAI-compatible vision endpoint.

The prompt is fixed. Captions written under different prompts are not
comparable inside one index, so changing PROMPT means recaptioning the
corpus, not patching it.
"""

import base64
import json
import time
import urllib.error
import urllib.request

from . import config

PROMPT = ("Describe this image in one or two sentences for a search index. "
          "Name the place, objects, and any visible text. "
          "Do not guess identities.")
MAX_TOKENS = 120
TEMPERATURE = 0.2


class CaptionError(Exception):
    pass


def payload(jpeg, model):
    uri = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
    return {"model": model, "max_tokens": MAX_TOKENS,
            "temperature": TEMPERATURE,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": uri}},
                {"type": "text", "text": PROMPT}]}]}


def caption(jpeg, url=None, model=None, retries=3, timeout=300,
            sleep=time.sleep):
    url = url or config.VISION_URL
    model = model or config.VISION_MODEL
    if not url:
        raise CaptionError("RECALL_VISION_URL is not set; see docs/sources.md")
    body = json.dumps(payload(jpeg, model)).encode()
    last = "no attempt made"
    for attempt in range(retries):
        req = urllib.request.Request(url, body,
                                     {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                text = json.load(r)["choices"][0]["message"]["content"].strip()
            if text:
                return text
            last = "the model returned an empty caption"
        except (urllib.error.URLError, OSError, KeyError, IndexError,
                ValueError) as e:
            last = f"{type(e).__name__}: {e}"
        if attempt + 1 < retries:
            sleep(2 ** attempt)
    raise CaptionError(last)
