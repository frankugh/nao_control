from __future__ import annotations

import re

MAX_SHORT_TOKENS = 5
MAX_COMBO_TOKENS = 8
TOKEN_RE = re.compile(r"\w+")

_COMBO_GAP = r"(?:\W+\w+){0,3}\W+"

STOP_PHRASE_RULES = [
    r"\bhou\s+op\b",
    r"\bstop\s+ermee\b",
    r"\bsta\s+stil\b",
    r"\bblijf\s+staan\b",
    r"\bkap\s+ermee\b",
    r"\bannuleer\b",
    r"\bniet\s+doen\b",
    r"\bga\s+niet\s+verder\b",
    r"\bniet\s+verder\b",
    r"\bho\s+nee\b",
    r"\bkappen\s+nu\b",
    r"\bkappen\s*,?\s*snel\s+nu\b",
]

STOP_SHORT_RULES = [
    r"\bstop\b",
    r"\bhalt\b",
    r"\bho\b",
    r"\bpauze\b",
    r"\bpauzeer\b",
    r"\bophouden\b",
    r"\bkappen\b",
]

STOP_COMBO_RULES = [
    rf"\bhelp\b{_COMBO_GAP}\bstop\b",
    rf"\boeps\b{_COMBO_GAP}\bstop\b",
    rf"\b(shit|kut|fuck)\b{_COMBO_GAP}\b(stop|kappen|hou\s+op|niet\s+doen)\b",
    rf"\b(stop|kappen|hou\s+op|niet\s+doen)\b{_COMBO_GAP}\b(shit|kut|fuck)\b",
]

STOP_RULES = STOP_PHRASE_RULES + STOP_SHORT_RULES + STOP_COMBO_RULES

_STOP_PHRASE_REGEXES = [re.compile(pattern, re.IGNORECASE) for pattern in STOP_PHRASE_RULES]
_STOP_SHORT_REGEXES = [re.compile(pattern, re.IGNORECASE) for pattern in STOP_SHORT_RULES]
_STOP_COMBO_REGEXES = [re.compile(pattern, re.IGNORECASE) for pattern in STOP_COMBO_RULES]


def stop_rules(text: str) -> bool:
    cleaned = " ".join(text.casefold().split())
    if any(regex.search(cleaned) for regex in _STOP_PHRASE_REGEXES):
        return True

    token_count = len(TOKEN_RE.findall(cleaned))
    if token_count <= MAX_SHORT_TOKENS and any(regex.search(cleaned) for regex in _STOP_SHORT_REGEXES):
        return True

    if token_count <= MAX_COMBO_TOKENS and any(regex.search(cleaned) for regex in _STOP_COMBO_REGEXES):
        return True

    return False
