from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, Optional

from dialog.interfaces import (
    CommandDecision,
    ICommandRecognizer,
    RouteDecision,
    parse_cmdrec_bundle,
)


def _resolve_existing_dir(path_str: str, candidates: list[Path]) -> Optional[Path]:
    raw = Path(path_str)
    if raw.is_dir():
        return raw.resolve()
    if raw.is_absolute():
        return None
    for base in candidates:
        p = base / raw
        if p.is_dir():
            return p.resolve()
    return None


def _candidate_bundle_roots(bundles_dir: str) -> list[Path]:
    raw = Path(bundles_dir)
    if raw.is_absolute():
        return [raw]

    dm_root = Path(__file__).resolve().parents[1]
    repo_root = dm_root.parent
    return [
        dm_root / raw,
        repo_root / raw,
        repo_root / "py3_command_recognition_train" / raw,
    ]


def _resolve_bundles_dir(bundles_dir: str) -> Path:
    candidates = _candidate_bundle_roots(bundles_dir)
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    return candidates[0].resolve()


def _list_bundle_dirs(bundles_dir: Path) -> list[Path]:
    if not bundles_dir.exists():
        return []
    return [p for p in bundles_dir.iterdir() if p.is_dir()]


def resolve_bundle_path(cmdrec_value: str, bundles_dir: str) -> Path:
    dm_root = Path(__file__).resolve().parents[1]
    repo_root = dm_root.parent
    candidate_roots = _candidate_bundle_roots(bundles_dir)
    explicit = _resolve_existing_dir(
        cmdrec_value,
        [dm_root, repo_root, Path.cwd(), *candidate_roots],
    )
    if explicit is not None:
        return explicit

    bundles_path = _resolve_bundles_dir(bundles_dir)
    available = _list_bundle_dirs(bundles_path)
    available_names = sorted(p.name for p in available)
    searched_roots = [p.resolve() for p in candidate_roots if p.exists()]
    if searched_roots and bundles_path not in searched_roots:
        searched_roots.append(bundles_path)

    value = cmdrec_value.strip().lower()
    if value == "latest":
        versioned: list[tuple[int, int, Path]] = []
        for path in available:
            match = re.match(r"bundle_v(\d+)(?:_(\d{8}))?$", path.name)
            if match:
                version = int(match.group(1))
                date_part = int(match.group(2)) if match.group(2) else 0
                versioned.append((version, date_part, path))
        if versioned:
            versioned.sort(key=lambda item: (item[0], item[1]), reverse=True)
            return versioned[0][2].resolve()
        raise ValueError(
            "Geen bundles gevonden voor cmdrec=latest. "
            f"Gezocht in {searched_roots or [bundles_path]}. "
            f"Beschikbaar: {available_names or 'geen'}."
        )

    match = re.match(r"^(?:bundle_)?v(\d+)(?:_(\d{8}))?$", value)
    if match:
        version = int(match.group(1))
        date_part = match.group(2)
        suffix = f"_{date_part}" if date_part else ""
        candidate = bundles_path / f"bundle_v{version}{suffix}"
        if candidate.is_dir():
            return candidate.resolve()
        raise ValueError(
            f"Bundle {candidate} niet gevonden. Beschikbaar: {available_names or 'geen'}."
        )

    raise ValueError(
        "cmdrec moet een bestaand pad zijn of 'latest'/'v<nummer>'/'bundle_v<nummer>'. "
        f"Gezocht in {searched_roots or [bundles_path]}. "
        f"Beschikbaar: {available_names or 'geen'}."
    )


class CmdRecRecognizer(ICommandRecognizer):
    def __init__(self, config: Dict[str, Any]):
        cmdrec_raw = parse_cmdrec_bundle(config.get("cmdrec", "none"))
        self._disabled = cmdrec_raw == "none"
        self._cmdrec_value = cmdrec_raw
        self._bundles_dir = config.get("cmdrec_bundles_dir", "dist")
        self._guarded_labels_override = config.get("guarded_labels_override", None)
        self._unguarded_labels_override = config.get("unguarded_labels_override", None)
        self._bundle_path: Optional[Path] = None
        self._model = None
        self._decision_policy: Optional[Dict[str, Any]] = None
        self._labels: Optional[list[str]] = None

        if not self._disabled:
            self._bundle_path = resolve_bundle_path(str(self._cmdrec_value), str(self._bundles_dir))

    @property
    def bundle_path(self) -> Optional[Path]:
        return self._bundle_path

    def _ensure_bundle_loaded(self) -> None:
        if self._model is not None:
            return
        if not self._bundle_path:
            raise RuntimeError("cmdrec bundle path ontbreekt; init eerst correct.")
        from cmdrec.predict import load_bundle

        self._model, self._decision_policy, self._labels = load_bundle(self._bundle_path)

    def get_labels(self) -> list[str]:
        if self._disabled:
            return []
        self._ensure_bundle_loaded()
        return list(self._labels or [])

    def get_dance_catalog(self) -> list[dict[str, Any]]:
        if self._disabled or not self._bundle_path:
            return []
        from cmdrec.resolvers import load_dance_catalog

        catalog_path = self._bundle_path / "dance_catalog.json"
        if not catalog_path.exists():
            dm_root = Path(__file__).resolve().parents[1]
            repo_root = dm_root.parent
            fallback = repo_root / "py3_command_recognition_train" / "data" / "dance_catalog.json"
            if fallback.exists():
                catalog_path = fallback
        if catalog_path.exists():
            return load_dance_catalog(catalog_path)
        return []

    def is_guarded(self, label: str) -> bool:
        if self._disabled:
            return False
        self._ensure_bundle_loaded()
        from cmdrec.guard import is_guarded

        policy = self._decision_policy or {}
        return is_guarded(
            label,
            policy,
            guarded_labels_override=self._guarded_labels_override,
            unguarded_labels_override=self._unguarded_labels_override,
        )

    def route(self, text: str, mode: str, active_behavior: Optional[str]) -> RouteDecision:
        if self._disabled:
            return RouteDecision(is_command=False, reason="disabled", top3=None)

        self._ensure_bundle_loaded()
        assert self._model is not None
        assert self._decision_policy is not None
        assert self._labels is not None

        from cmdrec.predict import decide, top_k
        from cmdrec.resolvers import load_dance_catalog, resolve_dance, resolve_locomotion
        from cmdrec.rules import stop_rules

        proba = self._model.predict_proba([text])[0]
        top3_items = top_k(proba, self._labels)
        top3 = [(item["label"], float(item["score"])) for item in top3_items]

        threshold = float(self._decision_policy["threshold"])
        margin = float(self._decision_policy["margin"])
        ml_decision = decide(proba, self._labels, threshold, margin)
        final_decision = "STOP" if stop_rules(text) else ml_decision

        if final_decision == "NONE":
            return RouteDecision(is_command=False, reason="low_confidence", top3=top3)

        label_scores = {label: float(score) for label, score in top3}
        confidence = label_scores.get(final_decision, top3[0][1] if top3 else 0.0)

        resolved: Optional[Dict[str, str]] = None
        if final_decision == "DANCE" and self._bundle_path:
            catalog_path = self._bundle_path / "dance_catalog.json"
            if not catalog_path.exists():
                dm_root = Path(__file__).resolve().parents[1]
                repo_root = dm_root.parent
                fallback = repo_root / "py3_command_recognition_train" / "data" / "dance_catalog.json"
                if fallback.exists():
                    catalog_path = fallback
            if catalog_path.exists():
                dance_catalog = load_dance_catalog(catalog_path)
                dance_key = resolve_dance(text, dance_catalog)
                dance_behavior = None
                for dance in dance_catalog:
                    if dance.get("key") == dance_key:
                        dance_behavior = dance.get("behavior")
                        break
                resolved = {"dance_key": dance_key}
                if isinstance(dance_behavior, str) and dance_behavior.strip():
                    resolved["dance_behavior"] = dance_behavior
        elif final_decision == "LOCOMOTION_REQUEST":
            locomotion = resolve_locomotion(text).get("action", "unknown")
            resolved = {"locomotion": locomotion}

        return RouteDecision(
            is_command=True,
            command=CommandDecision(
                label=final_decision,
                confidence=confidence,
                raw_text=text,
                resolved=resolved,
            ),
            reason=None,
            top3=top3,
        )


def route_decision_to_dict(decision: RouteDecision) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "is_command": decision.is_command,
        "top3": decision.top3,
        "command": None,
    }
    if decision.is_command:
        if decision.command:
            payload["command"] = {
                "label": decision.command.label,
                "confidence": decision.command.confidence,
                "raw_text": decision.command.raw_text,
                "resolved": decision.command.resolved,
            }
    else:
        payload["reason"] = decision.reason or "none"
    return payload


def _main() -> None:
    parser = argparse.ArgumentParser(description="CmdRec recognizer harness.")
    parser.add_argument("--cmdrec", default="latest")
    parser.add_argument("--bundles-dir", default="dist")
    parser.add_argument("--mode", default="default")
    parser.add_argument("--active-behavior", default=None)
    parser.add_argument("--text", required=True)
    args = parser.parse_args()

    recognizer = CmdRecRecognizer(
        {"cmdrec": args.cmdrec, "cmdrec_bundles_dir": args.bundles_dir}
    )
    decision = recognizer.route(args.text, args.mode, args.active_behavior)
    payload = route_decision_to_dict(decision)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()
