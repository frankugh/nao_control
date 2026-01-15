from __future__ import annotations

STOP_RULES = [
    "stop",
    "hou op",
    "stop ermee",
    "sta stil",
    "blijf staan",
    "halt",
    "ho",
]


def stop_rules(text: str) -> bool:
    cleaned = text.casefold()
    return any(rule in cleaned for rule in STOP_RULES)
