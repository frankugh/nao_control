from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import joblib
import numpy as np

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


def main() -> None:
    parser = argparse.ArgumentParser(description="Predict command label.")
    parser.add_argument("text", help="Input text")
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    args = parser.parse_args()

    bundle_path = Path(os.environ.get("CMDREC_BUNDLE", "dist/bundle_v1"))
    model, decision_policy, labels = load_bundle(bundle_path)

    proba = model.predict_proba([args.text])[0]
    top3 = top_k(proba, labels)
    threshold = decision_policy["threshold"]
    margin = decision_policy["margin"]

    ml_decision = decide(proba, labels, threshold, margin)
    final_decision = "STOP" if stop_rules(args.text) else ml_decision

    output = {
        "text": args.text,
        "top3": top3,
        "ml_decision": ml_decision,
        "final_decision": final_decision,
        "threshold": threshold,
        "margin": margin,
        "stop_rule_fired": stop_rules(args.text),
    }

    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return

    print("Top-3:")
    for item in top3:
        print(f"- {item['label']}: {item['score']:.3f}")
    print(f"ML decision: {ml_decision}")
    print(f"Final decision: {final_decision}")


if __name__ == "__main__":
    main()
