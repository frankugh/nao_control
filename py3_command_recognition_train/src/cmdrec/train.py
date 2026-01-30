from __future__ import annotations

import argparse
import copy
import json
import logging
import random
import shutil
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


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        entry: dict[str, Any] = {"text": payload["text"], "label": payload["label"]}
        if "source" in payload:
            entry["source"] = payload["source"]
        entries.append(entry)
    return entries


def normalize_none_source(entry: dict[str, Any]) -> str:
    source = str(entry.get("source") or "hf").casefold()
    if source not in {"seed", "candidates", "hf"}:
        return "hf"
    return source


def count_sources(entries: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        source = normalize_none_source(entry)
        counts[source] = counts.get(source, 0) + 1
    return counts


def cap_none_entries(
    none_entries: list[dict[str, Any]],
    max_none: int,
    seed: int = 42,
) -> list[dict[str, Any]]:
    seed_entries = [entry for entry in none_entries if normalize_none_source(entry) == "seed"]
    candidate_entries = [entry for entry in none_entries if normalize_none_source(entry) == "candidates"]
    hf_entries = [entry for entry in none_entries if normalize_none_source(entry) == "hf"]
    required = len(seed_entries) + len(candidate_entries)
    if max_none < required:
        LOGGER.warning(
            "NONE cap %s below seed+candidates %s; keeping all seed/candidates.",
            max_none,
            required,
        )
        max_none = required
    remaining = max_none - required
    if remaining < len(hf_entries):
        rng = random.Random(seed)
        hf_entries = rng.sample(hf_entries, k=remaining)
    return seed_entries + candidate_entries + hf_entries


def split_entries(entries: list[dict[str, Any]], val_frac: float, seed: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not entries:
        return [], []
    rng = random.Random(seed)
    indices = list(range(len(entries)))
    rng.shuffle(indices)
    val_count = max(1, int(round(len(entries) * val_frac)))
    if val_count >= len(entries):
        val_count = len(entries)
    val_entries = [entries[i] for i in indices[:val_count]]
    train_entries = [entries[i] for i in indices[val_count:]]
    return train_entries, val_entries


def compute_none_fp_rates(
    y_true: list[str],
    y_pred_system: list[str],
    sources: list[str | None],
) -> dict[str, float]:
    hard_sources = {"seed", "candidates"}
    hard_total = 0
    hard_fp = 0
    easy_total = 0
    easy_fp = 0
    for label, pred, source in zip(y_true, y_pred_system, sources):
        if label != "NONE" or source is None:
            continue
        if source in hard_sources:
            hard_total += 1
            if pred != "NONE":
                hard_fp += 1
        elif source == "hf":
            easy_total += 1
            if pred != "NONE":
                easy_fp += 1
    fp_none_rate_hard = hard_fp / hard_total if hard_total else 0.0
    fp_none_rate_easy = easy_fp / easy_total if easy_total else 0.0
    return {
        "fp_none_rate_hard": fp_none_rate_hard,
        "fp_none_rate_easy": fp_none_rate_easy,
    }


def decision_from_proba(proba_row: np.ndarray, classes: np.ndarray, threshold: float, margin: float) -> str:
    top_indices = np.argsort(proba_row)[::-1]
    top_idx = top_indices[0]
    top_prob = proba_row[top_idx]
    second_prob = proba_row[top_indices[1]] if len(top_indices) > 1 else 0.0
    if top_prob < threshold or (top_prob - second_prob) < margin:
        return "NONE"
    return classes[top_idx]


def build_error_records(
    texts: list[str],
    y_true: list[str],
    proba: np.ndarray,
    classes: np.ndarray,
    threshold: float,
    margin: float,
) -> list[dict[str, Any]]:
    label_to_index = {label: idx for idx, label in enumerate(classes)}
    records: list[dict[str, Any]] = []
    for text, true_label, row in zip(texts, y_true, proba):
        top_indices = np.argsort(row)[::-1]
        top_idx = int(top_indices[0])
        top_prob = float(row[top_idx])
        ml_pred_label = decision_from_proba(row, classes, threshold, margin)
        true_idx = label_to_index.get(true_label)
        true_prob = float(row[true_idx]) if true_idx is not None else 0.0
        top3 = [[classes[int(idx)], float(row[int(idx)])] for idx in top_indices[:3]]
        stop_hit = stop_rules(text)
        system_pred_label = "STOP" if stop_hit else ml_pred_label
        records.append(
            {
                "text": text,
                "true_label": true_label,
                "ml_pred_label": ml_pred_label,
                "ml_pred_proba": top_prob,
                "ml_true_proba": true_prob,
                "top3": top3,
                "system_pred_label": system_pred_label,
                "stop_rule_hit": stop_hit,
                "threshold": threshold,
                "margin": margin,
            }
        )
    return records


def cap_error_cases(
    records: list[dict[str, Any]],
    seed: int = 42,
    per_class: int = 300,
    max_total: int = 2000,
) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    by_label: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_label.setdefault(record["true_label"], []).append(record)
    capped: list[dict[str, Any]] = []
    for items in by_label.values():
        if len(items) > per_class:
            items = rng.sample(items, k=per_class)
        capped.extend(items)
    if len(capped) > max_total:
        capped.sort(key=lambda item: item["ml_pred_proba"], reverse=True)
        capped = capped[:max_total]
    return capped


def build_error_summary(
    y_true: list[str],
    ml_preds: list[str],
    system_preds: list[str],
) -> dict[str, Any]:
    def count_labels(items: Iterable[str]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in items:
            counts[item] = counts.get(item, 0) + 1
        return counts

    def confusion_counts(targets: list[str], preds: list[str]) -> dict[str, dict[str, int]]:
        matrix: dict[str, dict[str, int]] = {}
        for true_label, pred_label in zip(targets, preds):
            row = matrix.setdefault(true_label, {})
            row[pred_label] = row.get(pred_label, 0) + 1
        return matrix

    return {
        "true_label_counts": count_labels(y_true),
        "ml_pred_label_counts": count_labels(ml_preds),
        "system_pred_label_counts": count_labels(system_preds),
        "ml_confusion_counts": confusion_counts(y_true, ml_preds),
        "system_confusion_counts": confusion_counts(y_true, system_preds),
    }


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


def describe_vectorizer(vectorizer: Any) -> dict[str, Any]:
    if isinstance(vectorizer, TfidfVectorizer):
        return {
            "type": "tfidf",
            "analyzer": vectorizer.analyzer,
            "ngram_range": vectorizer.ngram_range,
        }
    if isinstance(vectorizer, FeatureUnion):
        return {
            "type": "feature_union",
            "transformers": [
                {"name": name, "config": describe_vectorizer(transformer)}
                for name, transformer in vectorizer.transformer_list
            ],
        }
    return {"type": vectorizer.__class__.__name__}


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
    parser.add_argument("--export-errors", dest="export_errors", action="store_true", default=True)
    parser.add_argument("--no-export-errors", dest="export_errors", action="store_false")
    args = parser.parse_args()

    none_path = select_none_path(args.none)

    command_entries = load_jsonl(args.commands)
    none_entries = load_jsonl(none_path)
    for entry in none_entries:
        if entry.get("label") == "NONE":
            entry["source"] = normalize_none_source(entry)

    max_none = int(len(command_entries) * 7)
    min_none = len(command_entries) * 2
    LOGGER.info("NONE source counts before cap: %s", count_sources(none_entries))
    if len(none_entries) > max_none:
        none_entries = cap_none_entries(none_entries, max_none)
        LOGGER.info("Capped NONE samples to %s (7x commands).", len(none_entries))
        LOGGER.info("NONE source counts after cap: %s", count_sources(none_entries))
    elif len(none_entries) < min_none:
        LOGGER.warning("NONE samples below 2x commands (%s vs %s).", len(none_entries), min_none)
        LOGGER.info("NONE source counts after cap: %s", count_sources(none_entries))
    else:
        LOGGER.info("NONE source counts after cap: %s", count_sources(none_entries))

    command_texts = [entry["text"] for entry in command_entries]
    command_labels = [entry["label"] for entry in command_entries]

    cmd_train_texts, cmd_val_texts, cmd_train_labels, cmd_val_labels = train_test_split(
        command_texts,
        command_labels,
        test_size=0.2,
        random_state=42,
        stratify=command_labels,
    )

    none_by_source = {"seed": [], "candidates": [], "hf": []}
    for entry in none_entries:
        none_by_source[normalize_none_source(entry)].append(entry)

    none_train_entries: list[dict[str, Any]] = []
    none_val_entries: list[dict[str, Any]] = []
    source_fracs = {"seed": 0.30, "candidates": 0.30, "hf": 0.15}
    source_seeds = {"seed": 43, "candidates": 44, "hf": 45}
    for source, entries in none_by_source.items():
        train_entries, val_entries = split_entries(entries, source_fracs[source], seed=source_seeds[source])
        none_train_entries.extend(train_entries)
        none_val_entries.extend(val_entries)

    x_train = cmd_train_texts + [entry["text"] for entry in none_train_entries]
    y_train = cmd_train_labels + [entry["label"] for entry in none_train_entries]
    x_val = cmd_val_texts + [entry["text"] for entry in none_val_entries]
    y_val = cmd_val_labels + [entry["label"] for entry in none_val_entries]
    val_sources = [None] * len(cmd_val_texts) + [entry.get("source") for entry in none_val_entries]

    none_source_counts = {
        "train": count_sources(none_train_entries),
        "val": count_sources(none_val_entries),
    }

    all_entries = command_entries + none_entries
    texts = [entry["text"] for entry in all_entries]
    labels = [entry["label"] for entry in all_entries]

    thresholds = np.arange(0.50, 0.95, 0.05)
    margins = np.arange(0.05, 0.30, 0.05)

    constraints = {
        "system_stop_recall": 0.95,
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
                system_metrics.update(compute_none_fp_rates(y_val, system_preds, val_sources))
                constraints_ok = (
                    system_metrics["system_stop_recall"] >= constraints["system_stop_recall"]
                    and system_metrics["fp_none_rate"] <= constraints["fp_none_rate"]
                )

                score = (
                    0.70 * system_metrics["macro_f1_others"]
                    + 0.15 * system_metrics["recall_box"]
                    + 0.15 * system_metrics["recall_dance"]
                    - 1.0 * system_metrics["fp_none_rate"]
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

    args.out.mkdir(parents=True, exist_ok=True)

    error_export = {
        "ml_error_count": 0,
        "system_error_count": 0,
        "error_cases_ml_path": None,
        "error_cases_system_path": None,
        "error_summary_path": None,
    }
    if args.export_errors:
        error_pipeline = Pipeline(
            [
                ("vectorizer", copy.deepcopy(best.vectorizer)),
                ("clf", LogisticRegression(max_iter=2000, class_weight="balanced")),
            ]
        )
        error_pipeline.fit(x_train, y_train)
        val_proba = error_pipeline.predict_proba(x_val)
        classes = error_pipeline.named_steps["clf"].classes_
        records = build_error_records(x_val, y_val, val_proba, classes, best.threshold, best.margin)
        ml_errors = [record for record in records if record["ml_pred_label"] != record["true_label"]]
        system_errors = [record for record in records if record["system_pred_label"] != record["true_label"]]

        ml_errors = cap_error_cases(ml_errors)
        system_errors = cap_error_cases(system_errors)

        ml_preds = [record["ml_pred_label"] for record in records]
        system_preds = [record["system_pred_label"] for record in records]
        error_summary = build_error_summary(y_val, ml_preds, system_preds)

        ml_error_path = args.out / "error_cases_ml.json"
        system_error_path = args.out / "error_cases_system.json"
        summary_path = args.out / "error_summary.json"
        ml_error_path.write_text(json.dumps(ml_errors, ensure_ascii=False, indent=2), encoding="utf-8")
        system_error_path.write_text(json.dumps(system_errors, ensure_ascii=False, indent=2), encoding="utf-8")
        summary_path.write_text(json.dumps(error_summary, ensure_ascii=False, indent=2), encoding="utf-8")

        error_export.update(
            {
                "ml_error_count": len(ml_errors),
                "system_error_count": len(system_errors),
                "error_cases_ml_path": str(ml_error_path),
                "error_cases_system_path": str(system_error_path),
                "error_summary_path": str(summary_path),
            }
        )
        LOGGER.info(
            "Wrote %s ML errors and %s SYSTEM errors to %s",
            len(ml_errors),
            len(system_errors),
            args.out,
        )

    final_pipeline = Pipeline(
        [
            ("vectorizer", copy.deepcopy(best.vectorizer)),
            ("clf", LogisticRegression(max_iter=2000, class_weight="balanced")),
        ]
    )
    final_pipeline.fit(texts, labels)

    joblib.dump(final_pipeline, args.out / "model.joblib")

    label_list = list(final_pipeline.named_steps["clf"].classes_)
    (args.out / "labels.json").write_text(json.dumps(label_list, ensure_ascii=False, indent=2), encoding="utf-8")

    decision_policy = {
        "threshold": best.threshold,
        "margin": best.margin,
        "labels": label_list,
        "stop_rules": STOP_RULES,
        "constraints": constraints,
        "vectorizer": best.name,
        "vectorizer_config": describe_vectorizer(best.vectorizer),
        "guard": {
            "default": True,
            "unguarded_labels": [
                "BOX",
                "HIGH_FIVE",
                "STOP",
                "WAVE",
                "SITDOWN",
                "STAND_UP",
                "REST",
            ],
            "guarded_labels": [
                "DANCE",
                "WALK_WITH_ME",
                "LOCOMOTION_REQUEST",
            ],
        },
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
            "none_path": str(none_path),
            "none_sources": none_source_counts,
        },
        "constraints": constraints,
        **error_export,
    }
    (args.out / "train_report.json").write_text(
        json.dumps(train_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    dance_catalog = Path("data/dance_catalog.json")
    if dance_catalog.exists():
        shutil.copy2(dance_catalog, args.out / "dance_catalog.json")
    else:
        LOGGER.warning("dance_catalog.json not found at %s", dance_catalog)

    dance_followup_policy = Path("data/dance_followup_policy.json")
    if dance_followup_policy.exists():
        shutil.copy2(dance_followup_policy, args.out / "dance_followup_policy.json")
    else:
        LOGGER.warning("dance_followup_policy.json not found at %s", dance_followup_policy)

    LOGGER.info("Training complete.")
    LOGGER.info("Best vectorizer: %s", best.name)
    LOGGER.info("Threshold=%.2f Margin=%.2f Score=%.3f", best.threshold, best.margin, best.score)
    LOGGER.info(
        "Score components: macro_f1_others=%.3f recall_box=%.3f recall_dance=%.3f fp_none_rate=%.3f",
        best.system_metrics["macro_f1_others"],
        best.system_metrics["recall_box"],
        best.system_metrics["recall_dance"],
        best.system_metrics["fp_none_rate"],
    )
    LOGGER.info("Bundle written to %s", args.out)


if __name__ == "__main__":
    main()
