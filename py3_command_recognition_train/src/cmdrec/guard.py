from __future__ import annotations

from typing import Any, Iterable


def _normalize_labels(labels: Iterable[str]) -> set[str]:
    normalized: set[str] = set()
    for label in labels:
        raw = str(label).strip()
        if not raw:
            continue
        raw = raw.replace("\\_", "_").replace("\\", "")
        normalized.add(raw.upper())
    return normalized


def is_guarded(
    label: str,
    decision_policy: dict[str, Any],
    *,
    guarded_labels_override: list[str] | None = None,
    unguarded_labels_override: list[str] | None = None,
) -> bool:
    normalized_label = str(label).strip()
    if not normalized_label:
        return True
    normalized_label = normalized_label.replace("\\_", "_").replace("\\", "").upper()

    if guarded_labels_override is not None:
        guarded = _normalize_labels(guarded_labels_override)
        return normalized_label in guarded

    if unguarded_labels_override is not None:
        unguarded = _normalize_labels(unguarded_labels_override)
        return normalized_label not in unguarded

    guard = decision_policy.get("guard")
    if not guard or not isinstance(guard, dict):
        return True

    default_guard = guard.get("default", True)
    if not isinstance(default_guard, bool):
        default_guard = True

    guarded_labels = guard.get("guarded_labels") or []
    unguarded_labels = guard.get("unguarded_labels") or []
    guarded = _normalize_labels(guarded_labels)
    unguarded = _normalize_labels(unguarded_labels)

    if default_guard:
        if unguarded:
            return normalized_label not in unguarded
        return True

    if guarded:
        return normalized_label in guarded
    return False
