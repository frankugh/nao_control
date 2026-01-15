from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _normalize(text: str) -> str:
    return " ".join(text.casefold().split())


def resolve_locomotion(text: str) -> dict[str, str]:
    cleaned = _normalize(text)

    if any(phrase in cleaned for phrase in ["stoppen met lopen", "klaar", "stop met lopen"]):
        return {"action": "exit"}

    if "draai" in cleaned or "draait" in cleaned:
        if "links" in cleaned:
            return {"action": "turn_left"}
        if "rechts" in cleaned:
            return {"action": "turn_right"}
        return {"action": "turn_left"}

    if any(phrase in cleaned for phrase in ["loop naar links", "stap naar links", "naar links"]):
        return {"action": "left"}
    if any(phrase in cleaned for phrase in ["loop naar rechts", "stap naar rechts", "naar rechts"]):
        return {"action": "right"}

    if any(phrase in cleaned for phrase in ["vooruit", "naar voren", "voorwaarts"]):
        return {"action": "forward"}
    if any(phrase in cleaned for phrase in ["achteruit", "terug", "stukje terug"]):
        return {"action": "back"}

    return {"action": "unknown"}


def load_dance_catalog(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("dances", [])


def resolve_dance(text: str, dance_catalog: list[dict[str, Any]]) -> str:
    cleaned = _normalize(text)
    for dance in dance_catalog:
        aliases = dance.get("aliases", [])
        if any(alias.casefold() in cleaned for alias in aliases):
            return dance.get("key", "unknown")
    return "unknown"
