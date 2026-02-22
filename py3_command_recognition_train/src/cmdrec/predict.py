from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

import joblib
import numpy as np

from cmdrec.resolvers import load_dance_catalog, resolve_dance
from cmdrec.rules import stop_rules


def load_bundle(bundle_path: Path) -> tuple[Any, dict[str, Any], list[str]]:
    model = joblib.load(bundle_path / "model.joblib")
    decision_policy = json.loads((bundle_path / "decision_policy.json").read_text(encoding="utf-8"))
    labels = json.loads((bundle_path / "labels.json").read_text(encoding="utf-8"))
    return model, decision_policy, labels


def top_k(proba: np.ndarray, labels: list[str], k: int = 3) -> list[dict[str, float]]:
    indices = np.argsort(proba)[::-1][:k]
    return [{"label": labels[idx], "score": float(proba[idx])} for idx in indices]


def decide(proba: np.ndarray, labels: list[str], threshold: float, margin: float) -> str:
    order = np.argsort(proba)[::-1]
    top_idx = order[0]
    top_score = proba[top_idx]
    second_score = proba[order[1]] if len(order) > 1 else 0.0
    if top_score < threshold or (top_score - second_score) < margin:
        return "NONE"
    return labels[top_idx]


def stop_rule_mode(decision_policy: dict[str, Any]) -> str:
    raw = str(decision_policy.get("stop_rule_mode", "preemptive") or "preemptive").strip().lower()
    if raw in {"off", "none", "disabled", "false", "0"}:
        return "off"
    return "preemptive"


def classify_text(
    model: Any,
    decision_policy: dict[str, Any],
    labels: list[str],
    text: str,
    *,
    k: int = 3,
) -> dict[str, Any]:
    threshold = float(decision_policy["threshold"])
    margin = float(decision_policy["margin"])
    mode = stop_rule_mode(decision_policy)

    # Safety fast-path from decision policy: can bypass ML inference entirely.
    if mode != "off" and stop_rules(text):
        return {
            "top3": [{"label": "STOP", "score": 1.0}],
            "ml_decision": None,
            "final_decision": "STOP",
            "threshold": threshold,
            "margin": margin,
            "stop_rule_fired": True,
            "inference_skipped": True,
            "stop_rule_mode": mode,
        }

    proba = model.predict_proba([text])[0]
    top3 = top_k(proba, labels, k=k)
    ml_decision = decide(proba, labels, threshold, margin)
    return {
        "top3": top3,
        "ml_decision": ml_decision,
        "final_decision": ml_decision,
        "threshold": threshold,
        "margin": margin,
        "stop_rule_fired": False,
        "inference_skipped": False,
        "stop_rule_mode": mode,
    }


def find_latest_bundle(root: Path) -> Path | None:
    candidates = [path for path in root.glob("bundle_v*") if path.is_dir()]
    if not candidates:
        return None
    versioned: list[tuple[int, Path]] = []
    fallback: list[Path] = []
    for path in candidates:
        match = re.match(r"bundle_v(\d+)$", path.name)
        if match:
            versioned.append((int(match.group(1)), path))
        else:
            fallback.append(path)
    if versioned:
        versioned.sort(key=lambda item: item[0], reverse=True)
        return versioned[0][1]
    fallback.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return fallback[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="Predict command label.")
    parser.add_argument("text", help="Input text")
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    args = parser.parse_args()

    bundle_env = os.environ.get("CMDREC_BUNDLE")
    if bundle_env:
        bundle_path = Path(bundle_env)
    else:
        default_path = Path("dist/bundle_v1")
        bundle_path = default_path
        if not (bundle_path / "model.joblib").exists():
            latest = find_latest_bundle(Path("dist"))
            if latest is not None:
                bundle_path = latest
    model, decision_policy, labels = load_bundle(bundle_path)

    classification = classify_text(model, decision_policy, labels, args.text)
    top3 = classification["top3"]
    threshold = classification["threshold"]
    margin = classification["margin"]
    ml_decision = classification["ml_decision"]
    final_decision = classification["final_decision"]

    resolved_dance = None
    if final_decision == "DANCE":
        catalog_path = bundle_path / "dance_catalog.json"
        if not catalog_path.exists():
            catalog_path = Path("data/dance_catalog.json")
        if catalog_path.exists():
            dance_catalog = load_dance_catalog(catalog_path)
            resolved_dance = resolve_dance(args.text, dance_catalog)

    output = {
        "text": args.text,
        "top3": top3,
        "ml_decision": ml_decision,
        "final_decision": final_decision,
        "threshold": threshold,
        "margin": margin,
        "stop_rule_fired": classification["stop_rule_fired"],
        "stop_rule_mode": classification["stop_rule_mode"],
        "inference_skipped": classification["inference_skipped"],
        "resolved_dance": resolved_dance,
    }

    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return

    print("Top-3:")
    for item in top3:
        print(f"- {item['label']}: {item['score']:.3f}")
    print(f"ML decision: {ml_decision if ml_decision is not None else '<skipped>'}")
    print(f"Final decision: {final_decision}")
    if resolved_dance is not None:
        print(f"Resolved dance: {resolved_dance}")


if __name__ == "__main__":
    main()
