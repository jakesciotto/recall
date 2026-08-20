"""Read a vCard export into an identifier-to-name map.

Drop any .vcf under the data directory and every adapter picks it up. On one
real corpus this named 52 percent of all chunks, and the single most frequent
number in that corpus appeared in 73,076 of them.

Export from the platform that actually holds your address book. The same
corpus matched 8 percent of chunks from one provider's export and 52 percent
from another, because the thin one was a stale partial copy.
"""

import re

from .sources.base import walk

# Five and six digit senders are short codes: banks, 2FA, marketing. They are
# not people, and normalising them would collide with real numbers.
MIN_DIGITS = 7


def normalize_phone(raw):
    """A phone number in the form the corpus stores, or None."""
    if not raw:
        return None
    explicit = raw.strip().startswith("+")
    digits = re.sub(r"\D", "", raw)
    if len(digits) < MIN_DIGITS:
        return None
    if explicit or len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    return "+" + digits


def parse(text):
    """[{name, phones, emails}] for every card carrying a name."""
    out, card = [], None
    for line in text.splitlines():
        line = line.strip()
        if line == "BEGIN:VCARD":
            card = {"name": None, "phones": [], "emails": []}
        elif line == "END:VCARD":
            if card and card["name"]:
                out.append(card)
            card = None
        elif card is not None and ":" in line:
            prop, _, value = line.partition(":")
            key, value = prop.split(";")[0].upper(), value.strip()
            if not value:
                continue
            if key == "FN":
                card["name"] = value
            elif key == "TEL":
                card["phones"].append(value)
            elif key == "EMAIL":
                card["emails"].append(value)
    return out


def build_map(cards):
    """{identifier: name}. First card wins a shared number, so the map does
    not move between runs."""
    out = {}
    for card in cards:
        for phone in card["phones"]:
            key = normalize_phone(phone)
            if key and key not in out:
                out[key] = card["name"]
        for email in card["emails"]:
            key = email.strip().lower()
            if key and key not in out:
                out[key] = card["name"]
    return out


def load(path):
    return build_map(parse(open(path, encoding="utf-8", errors="replace").read()))


def detect(root):
    return sorted(p for p in walk(root)
                  if p.name.lower().endswith(".vcf") and p.is_file())


def load_all(root):
    """Every vCard under the root, merged. Empty when none are present."""
    merged = {}
    for path in detect(root):
        for key, name in load(path).items():
            merged.setdefault(key, name)
    return merged
