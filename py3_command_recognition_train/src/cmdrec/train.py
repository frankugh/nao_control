from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import joblib
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report, confusion_matrix
from sklearn.model_selection import train_test_split
from sklearn.pipeline import FeatureUnion, Pipeline

from cmdrec.rules import STOP_RULES, stop_rules

LOGGER = logging.getLogger(__name__)


@dataclass
class CandidateResult:
    name: str
    vectorizer: Any
    threshold: float
    margin: float
    score: float
    ml_report: dict[str, Any]
    ml_confusion: list[list[int]]
    system_metrics: dict[str, Any]
    constraints_ok: bool


def load_jsonl(path: Path) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        entries.append({"text": payload["text"], "label": payload["label"]})
    return entries


def decision_from_proba(proba_row: np.ndarray, classes: np.ndarray, threshold: float, margin: float) -> str:
    top_indices = np.argsort(proba_row)[::-1]
    top_idx = top_indices[0]
    top_prob = proba_row[top_idx]
    second_prob = proba_row[top_indices[1]] if len(top_indices) > 1 else 0.0
    if top_prob < threshold or (top_prob - second_prob) < margin:
        return "NONE"
    return classes[top_idx]


def evaluate_candidate(
    name: str,
    pipeline: Pipeline,
    x_val: list[str],
    y_val: list[str],
    threshold: float,
    margin: float,
) -> tuple[dict[str, Any], list[list[int]], list[str], list[str]]:
    proba = pipeline.predict_proba(x_val)
    classes = pipeline.named_steps["clf"].classes_
    ml_preds = [decision_from_proba(row, classes, threshold, margin) for row in proba]

    ml_report = classification_report(y_val, ml_preds, output_dict=True, zero_division=0)
    labels = list(classes)
    ml_confusion = confusion_matrix(y_val, ml_preds, labels=labels).tolist()

    system_preds = ["STOP" if stop_rules(text) else pred for text, pred in zip(x_val, ml_preds)]

    return ml_report, ml_confusion, ml_preds, system_preds


def compute_system_metrics(
    y_true: list[str],
    y_pred: list[str],
    y_pred_system: list[str],
) -> dict[str, Any]:
    report = classification_report(y_true, y_pred_system, output_dict=True, zero_division=0)

    stop_total = sum(1 for label in y_true if label == "STOP")
    stop_correct = sum(1 for label, pred in zip(y_true, y_pred_system) if label == "STOP" and pred == "STOP")
    system_stop_recall = stop_correct / stop_total if stop_total else 0.0

    none_total = sum(1 for label in y_true if label == "NONE")
    none_pred_command = sum(1 for label, pred in zip(y_true, y_pred_system) if label == "NONE" and pred != "NONE")
    fp_none_rate = none_pred_command / none_total if none_total else 0.0

    none_pred_loco = sum(
        1 for label, pred in zip(y_true, y_pred_system) if label == "NONE" and pred == "LOCOMOTION_REQUEST"
    )
    fp_loco_rate = none_pred_loco / none_total if none_total else 0.0

    recall_dance = report.get("DANCE", {}).get("recall", 0.0)
    recall_box = report.get("BOX", {}).get("recall", 0.0)

    other_scores = [
        report[label]["f1-score"]
        for label in report
        if label not in {"accuracy", "macro avg", "weighted avg", "STOP", "LOCOMOTION_REQUEST"}
        and isinstance(report[label], dict)
    ]
    macro_f1_others = float(np.mean(other_scores)) if other_scores else 0.0

    return {
        "system_stop_recall": system_stop_recall,
        "fp_none_rate": fp_none_rate,
        "fp_loco_rate": fp_loco_rate,
        "recall_dance": recall_dance,
        "recall_box": recall_box,
        "macro_f1_others": macro_f1_others,
        "report": report,
    }


def build_vectorizers() -> list[tuple[str, Any]]:
    return [
        ("char_wb_3_5", TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5))),
        ("char_wb_2_5", TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5))),
        ("word_1_2", TfidfVectorizer(analyzer="word", ngram_range=(1, 2))),
        (
            "hybrid_word_char",
            FeatureUnion(
                [
                    ("word", TfidfVectorizer(analyzer="word", ngram_range=(1, 2))),
                    ("char", TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5))),
                ]
            ),
        ),
    ]


def select_none_path(path: Path | None) -> Path:
    if path is not None:
        return path
    clean_path = Path("data/none_clean.jsonl")
    if clean_path.exists():
        return clean_path
    return Path("data/none.jsonl")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="Train command recognition model.")
    parser.add_argument("--commands", type=Path, default=Path("data/commands.jsonl"))
    parser.add_argument("--none", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=Path("dist/bundle_v1"))
    args = parser.parse_args()

    none_path = select_none_path(args.none)

    command_entries = load_jsonl(args.commands)
    none_entries = load_jsonl(none_path)

    all_entries = command_entries + none_entries
    texts = [entry["text"] for entry in all_entries]
    labels = [entry["label"] for entry in all_entries]

    x_train, x_val, y_train, y_val = train_test_split(
        texts, labels, test_size=0.2, random_state=42, stratify=labels
    )

    thresholds = np.arange(0.50, 0.95, 0.05)
    margins = np.arange(0.05, 0.30, 0.05)

    stop_count = sum(1 for label in y_val if label == "STOP")
    stop_recall_constraint = 0.995 if stop_count >= 20 else 0.98
    if stop_count < 20:
        LOGGER.warning("STOP validation samples are low (%s). Using relaxed stop recall constraint.", stop_count)

    constraints = {
        "system_stop_recall": stop_recall_constraint,
        "fp_loco_rate": 0.005,
        "fp_none_rate": 0.02,
    }

    results: list[CandidateResult] = []
    for name, vectorizer in build_vectorizers():
        pipeline = Pipeline(
            [
                ("vectorizer", vectorizer),
                ("clf", LogisticRegression(max_iter=2000, class_weight="balanced")),
            ]
        )
        pipeline.fit(x_train, y_train)

        for threshold in thresholds:
            for margin in margins:
                ml_report, ml_confusion, ml_preds, system_preds = evaluate_candidate(
                    name, pipeline, x_val, y_val, threshold, margin
                )
                system_metrics = compute_system_metrics(y_val, ml_preds, system_preds)
                constraints_ok = (
                    system_metrics["system_stop_recall"] >= constraints["system_stop_recall"]
                    and system_metrics["fp_loco_rate"] <= constraints["fp_loco_rate"]
                    and system_metrics["fp_none_rate"] <= constraints["fp_none_rate"]
                )

                score = (
                    0.45 * system_metrics["recall_dance"]
                    + 0.45 * system_metrics["recall_box"]
                    + 0.10 * system_metrics["macro_f1_others"]
                    - 2.0 * system_metrics["fp_none_rate"]
                    - 4.0 * system_metrics["fp_loco_rate"]
                )

                results.append(
                    CandidateResult(
                        name=name,
                        vectorizer=vectorizer,
                        threshold=float(threshold),
                        margin=float(margin),
                        score=float(score),
                        ml_report=ml_report,
                        ml_confusion=ml_confusion,
                        system_metrics=system_metrics,
                        constraints_ok=constraints_ok,
                    )
                )

    constrained = [result for result in results if result.constraints_ok]
    best_pool = constrained or results
    if not constrained:
        LOGGER.warning("No candidate met constraints. Selecting best by score only.")

    best = max(best_pool, key=lambda result: result.score)

    final_pipeline = Pipeline(
        [
            ("vectorizer", best.vectorizer),
            ("clf", LogisticRegression(max_iter=2000, class_weight="balanced")),
        ]
    )
    final_pipeline.fit(texts, labels)

    args.out.mkdir(parents=True, exist_ok=True)
    joblib.dump(final_pipeline, args.out / "model.joblib")

    label_list = list(final_pipeline.named_steps["clf"].classes_)
    (args.out / "labels.json").write_text(json.dumps(label_list, ensure_ascii=False, indent=2), encoding="utf-8")

    decision_policy = {
        "threshold": best.threshold,
        "margin": best.margin,
        "stop_rules": STOP_RULES,
        "constraints": constraints,
        "vectorizer": best.name,
    }
    (args.out / "decision_policy.json").write_text(
        json.dumps(decision_policy, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    train_report = {
        "best": {
            "vectorizer": best.name,
            "threshold": best.threshold,
            "margin": best.margin,
            "score": best.score,
        },
        "ml_only": {
            "report": best.ml_report,
            "confusion_matrix": {
                "labels": label_list,
                "matrix": best.ml_confusion,
            },
        },
        "system_metrics": best.system_metrics,
        "data": {
            "commands": len(command_entries),
            "none": len(none_entries),
            "total": len(all_entries),
        },
        "constraints": constraints,
    }
    (args.out / "train_report.json").write_text(
        json.dumps(train_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    LOGGER.info("Training complete.")
    LOGGER.info("Best vectorizer: %s", best.name)
    LOGGER.info("Threshold=%.2f Margin=%.2f Score=%.3f", best.threshold, best.margin, best.score)
    LOGGER.info("Bundle written to %s", args.out)


if __name__ == "__main__":
    main()
