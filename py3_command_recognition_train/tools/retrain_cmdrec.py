#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import logging
import random
import shutil
import re
import time
import math
from pathlib import Path
from typing import Any, Iterable

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline

from cmdrec.commands import normalize_key
from cmdrec.rules import STOP_RULES
from cmdrec import train as train_mod

LOGGER = logging.getLogger(__name__)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return train_mod.load_jsonl(path)


def load_jsonl_raw(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not path.exists():
        return entries
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entries.append(json.loads(line))
    return entries


def write_jsonl(entries: Iterable[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def dedupe_entries(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for entry in entries:
        text = str(entry.get("text") or "")
        key = normalize_key(text)
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    return deduped


def merge_optional(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return load_jsonl(path)


def normalize_al_entries(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for entry in entries:
        label = entry.get("label")
        if not label:
            label = entry.get("suggested_label") or entry.get("predicted_label")
        if not label:
            continue
        normalized.append(
            {
                "text": entry.get("text", ""),
                "label": str(label),
                "confidence": entry.get("confidence"),
            }
        )
    return normalized


def select_auto_train_entries(
    entries: list[dict[str, Any]],
    sample_size: int,
    conf_min: float | None,
    conf_max: float | None,
) -> list[dict[str, Any]]:
    if sample_size <= 0:
        return []
    filtered: list[dict[str, Any]] = []
    for entry in entries:
        conf = entry.get("confidence")
        if conf is None:
            continue
        try:
            conf_value = float(conf)
        except (TypeError, ValueError):
            continue
        if conf_min is not None and conf_value < conf_min:
            continue
        if conf_max is not None and conf_value > conf_max:
            continue
        filtered.append({**entry, "confidence": conf_value})
    filtered.sort(key=lambda item: item["confidence"])
    if sample_size and len(filtered) > sample_size:
        filtered = filtered[:sample_size]
    return filtered


def resolve_auto_out(out_path: Path, baseline_root: Path, output_root: Path) -> Path:
    if out_path.as_posix().lower() != "auto":
        return out_path
    bundles = list_bundles(baseline_root)
    version = bundles[0][0] if bundles else 1
    date_stamp = time.strftime("%Y%m%d", time.gmtime())
    output_root.mkdir(parents=True, exist_ok=True)
    return output_root / f"bundle_v{version}_{date_stamp}"


def list_bundles(root: Path) -> list[tuple[int, int, Path]]:
    pattern = re.compile(r"^bundle_v(\d+)(?:_(\d{8}))?$")
    bundles: list[tuple[int, int, Path]] = []
    if not root.exists():
        return bundles
    for path in root.iterdir():
        if not path.is_dir():
            continue
        name = path.name
        if name.lower() in {"backup", "experiments"} or name.lower().startswith("v0"):
            continue
        match = pattern.match(name)
        if not match:
            continue
        version = int(match.group(1))
        date_part = int(match.group(2)) if match.group(2) else 0
        bundles.append((version, date_part, path))
    bundles.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return bundles


def load_train_report(bundle_path: Path) -> dict[str, Any] | None:
    report_path = bundle_path / "train_report.json"
    if not report_path.exists():
        return None
    try:
        return json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def constraints_ok(report: dict[str, Any]) -> bool:
    constraints = report.get("constraints", {})
    metrics = report.get("system_metrics", {})
    stop_req = float(constraints.get("system_stop_recall", 0.0))
    none_req = float(constraints.get("fp_none_rate", 1.0))
    return (
        float(metrics.get("system_stop_recall", 0.0)) >= stop_req
        and float(metrics.get("fp_none_rate", 1.0)) <= none_req
    )


def count_labels(entries: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        label = str(entry.get("label") or "")
        counts[label] = counts.get(label, 0) + 1
    return counts


def sample_external_none(
    external_entries: list[dict[str, Any]],
    excluded_keys: set[str],
    target_count: int,
    seed: int,
) -> list[dict[str, Any]]:
    candidates = [entry for entry in external_entries if normalize_key(entry["text"]) not in excluded_keys]
    if target_count <= 0 or not candidates:
        return []
    if len(candidates) <= target_count:
        return candidates
    rng = random.Random(seed)
    return rng.sample(candidates, k=target_count)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="Retrain cmdrec using fixed gold split.")
    parser.add_argument("--train-base", type=Path, default=Path("data/train_base.jsonl"))
    parser.add_argument("--gold", type=Path, default=Path("data/gold_v1.jsonl"))
    parser.add_argument("--none-external", type=Path, default=Path("data/none_external.jsonl"))
    parser.add_argument("--auto-train", type=Path, default=Path("data/al/auto_train.jsonl"))
    parser.add_argument(
        "--auto-train-queue", type=Path, default=Path("data/al/auto_train_queue.jsonl")
    )
    parser.add_argument("--reviewed", type=Path, default=Path("data/al/reviewed.jsonl"))
    parser.add_argument("--auto-train-sample", type=int, default=0)
    parser.add_argument("--auto-train-conf-min", type=float, default=None)
    parser.add_argument("--auto-train-conf-max", type=float, default=None)
    parser.add_argument(
        "--none-ratio",
        type=int,
        default=5,
        help="Target NONE ratio (NONE = ratio * commands).",
    )
    parser.add_argument(
        "--none-ratio-grid",
        type=str,
        default="1,2,3,5,8",
        help="Comma-separated NONE ratios to compare (overrides --none-ratio).",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=Path("dist/bundle_v17"))
    parser.add_argument("--auto-root", type=Path, default=Path("dist/experiments"))
    parser.add_argument("--promote-if-better", action="store_true", default=False)
    parser.add_argument("--export-errors", dest="export_errors", action="store_true", default=True)
    parser.add_argument("--no-export-errors", dest="export_errors", action="store_false")
    args = parser.parse_args()

    bundles_dir = Path("dist")
    out_auto = args.out.as_posix().lower() == "auto"
    args.out = resolve_auto_out(args.out, bundles_dir, args.auto_root)
    baseline_bundle = list_bundles(bundles_dir)
    baseline_path = baseline_bundle[0][2] if baseline_bundle else None

    gold_entries = dedupe_entries(load_jsonl(args.gold))
    gold_keys = {normalize_key(entry["text"]) for entry in gold_entries}

    base_entries = load_jsonl(args.train_base)
    reviewed_entries = merge_optional(args.reviewed)
    reviewed_count = len(reviewed_entries)

    auto_train_raw = load_jsonl_raw(args.auto_train)
    auto_train_raw = dedupe_entries(auto_train_raw)
    auto_queue_raw = load_jsonl_raw(args.auto_train_queue)
    auto_queue_raw = dedupe_entries(auto_queue_raw)

    existing_keys = {normalize_key(entry.get("text", "")) for entry in auto_train_raw}
    queue_candidates = [
        entry
        for entry in auto_queue_raw
        if normalize_key(entry.get("text", "")) not in existing_keys
    ]

    max_auto = int(math.floor(0.5 * reviewed_count))
    if args.auto_train_sample and args.auto_train_sample > 0:
        max_auto = min(max_auto, args.auto_train_sample)
    needed = max(0, max_auto - len(auto_train_raw))
    if needed > 0:
        selected = select_auto_train_entries(
            queue_candidates,
            needed,
            args.auto_train_conf_min,
            args.auto_train_conf_max,
        )
        if selected:
            selected_keys = {normalize_key(entry.get("text", "")) for entry in selected}
            auto_train_raw.extend(selected)
            auto_queue_raw = [
                entry
                for entry in auto_queue_raw
                if normalize_key(entry.get("text", "")) not in selected_keys
            ]
            write_jsonl(auto_train_raw, args.auto_train)
            write_jsonl(auto_queue_raw, args.auto_train_queue)

    auto_train_entries = normalize_al_entries(auto_train_raw)
    base_entries += auto_train_entries
    base_entries += reviewed_entries
    base_entries = dedupe_entries(base_entries)
    base_entries = [entry for entry in base_entries if normalize_key(entry["text"]) not in gold_keys]

    command_entries = [entry for entry in base_entries if entry.get("label") != "NONE"]
    none_seed_entries = [entry for entry in base_entries if entry.get("label") == "NONE"]

    external_none = merge_optional(args.none_external)

    ratios = [int(r.strip()) for r in args.none_ratio_grid.split(",") if r.strip()]
    if not ratios:
        ratios = [args.none_ratio]

    thresholds = np.arange(0.50, 0.95, 0.05)
    margins = np.arange(0.05, 0.30, 0.05)

    constraints = {
        "system_stop_recall": 0.95,
        "fp_none_rate": 0.05,
    }

    results: list[train_mod.CandidateResult] = []
    best_ratio: int | None = None
    best_data_counts: dict[str, int] = {}

    x_val = [entry["text"] for entry in gold_entries]
    y_val = [entry["label"] for entry in gold_entries]
    val_sources = [
        entry.get("source") if entry.get("label") == "NONE" else None for entry in gold_entries
    ]

    for ratio in ratios:
        target_none = ratio * len(command_entries)
        none_entries = list(none_seed_entries)
        if len(none_entries) < target_none:
            needed = target_none - len(none_entries)
            none_entries += sample_external_none(external_none, gold_keys, needed, seed=args.seed)

        x_train = [entry["text"] for entry in command_entries] + [entry["text"] for entry in none_entries]
        y_train = [entry["label"] for entry in command_entries] + [entry["label"] for entry in none_entries]

        LOGGER.info(
            "Ratio %s: train labels %s",
            ratio,
            count_labels(command_entries + none_entries),
        )
        LOGGER.info("Gold label counts: %s", count_labels(gold_entries))

        for name, vectorizer in train_mod.build_vectorizers():
            pipeline = Pipeline(
                [
                    ("vectorizer", vectorizer),
                    ("clf", LogisticRegression(max_iter=2000, class_weight="balanced")),
                ]
            )
            pipeline.fit(x_train, y_train)

            for threshold in thresholds:
                for margin in margins:
                    ml_report, ml_confusion, ml_preds, system_preds = train_mod.evaluate_candidate(
                        name, pipeline, x_val, y_val, threshold, margin
                    )
                    system_metrics = train_mod.compute_system_metrics(y_val, ml_preds, system_preds)
                    system_metrics.update(train_mod.compute_none_fp_rates(y_val, system_preds, val_sources))
                    constraints_ok_flag = (
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
                        train_mod.CandidateResult(
                            name=f"{name}|ratio={ratio}",
                            vectorizer=vectorizer,
                            threshold=float(threshold),
                            margin=float(margin),
                            score=float(score),
                            ml_report=ml_report,
                            ml_confusion=ml_confusion,
                            system_metrics=system_metrics,
                            constraints_ok=constraints_ok_flag,
                        )
                    )

    vectorizer_names = sorted({result.name.split("|ratio=")[0] for result in results})
    LOGGER.info(
        "Search space: vectorizers=%s ratios=%s thresholds=%s margins=%s candidates=%s",
        vectorizer_names,
        ratios,
        [float(v) for v in thresholds],
        [float(v) for v in margins],
        len(results),
    )

    constrained = [result for result in results if result.constraints_ok]
    best_pool = constrained or results
    if not constrained:
        LOGGER.warning("No candidate met constraints. Selecting best by score only.")
    best = max(best_pool, key=lambda result: result.score)
    if "|ratio=" in best.name:
        best_ratio = int(best.name.split("|ratio=")[-1])
    else:
        best_ratio = ratios[0]

    target_none = best_ratio * len(command_entries)
    none_entries = list(none_seed_entries)
    if len(none_entries) < target_none:
        needed = target_none - len(none_entries)
        none_entries += sample_external_none(external_none, gold_keys, needed, seed=args.seed)
    best_data_counts = count_labels(command_entries + none_entries)

    args.out.mkdir(parents=True, exist_ok=True)

    error_export = {
        "ml_error_count": 0,
        "system_error_count": 0,
        "system_disagreement_count": 0,
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
        records = train_mod.build_error_records(x_val, y_val, val_proba, classes, best.threshold, best.margin)
        ml_errors = [record for record in records if record["ml_pred_label"] != record["true_label"]]
        system_errors = train_mod.build_system_disagreement_cases(records)

        ml_errors = train_mod.cap_error_cases(ml_errors)
        system_errors = train_mod.cap_error_cases(system_errors)

        ml_preds = [record["ml_pred_label"] for record in records]
        system_preds = [record["system_pred_label"] for record in records]
        error_summary = train_mod.build_error_summary(y_val, ml_preds, system_preds)

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
                "system_disagreement_count": len(system_errors),
                "error_cases_ml_path": str(ml_error_path),
                "error_cases_system_path": str(system_error_path),
                "error_summary_path": str(summary_path),
            }
        )
        LOGGER.info(
            "Wrote %s ML errors and %s SYSTEM-vs-ML disagreements to %s",
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
    final_pipeline.fit(x_train, y_train)

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
        "vectorizer_config": train_mod.describe_vectorizer(best.vectorizer),
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

    score_formula = (
        "0.70*macro_f1_others + 0.15*recall_box + 0.15*recall_dance - 1.0*fp_none_rate"
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
            "total": len(command_entries) + len(none_entries),
            "gold": len(gold_entries),
            "none_external_path": str(args.none_external),
            "none_ratio": best_ratio,
            "train_label_counts": best_data_counts,
        },
        "gold_label_counts": count_labels(gold_entries),
        "constraints": constraints,
        "search_space": {
            "ratios": ratios,
            "thresholds": [float(v) for v in thresholds],
            "margins": [float(v) for v in margins],
            "vectorizers": vectorizer_names,
            "candidates": len(results),
        },
        "validation": {"gold_path": str(args.gold), "gold_count": len(gold_entries)},
        "score_formula": score_formula,
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

    if args.promote_if_better and out_auto:
        new_report = load_train_report(args.out)
        old_report = load_train_report(baseline_path) if baseline_path else None
        if not new_report:
            LOGGER.warning("No train_report.json found for %s; skipping promotion.", args.out)
        elif not old_report:
            LOGGER.warning("No baseline report found; skipping promotion.")
        else:
            new_score = float(new_report.get("best", {}).get("score", 0.0))
            old_score = float(old_report.get("best", {}).get("score", 0.0))
            if new_score >= old_score and constraints_ok(new_report):
                dest = bundles_dir / args.out.name
                if dest.exists():
                    LOGGER.warning("Promotion target exists: %s; skipping.", dest)
                else:
                    shutil.move(str(args.out), dest)
                    LOGGER.info("Promoted bundle to %s", dest)
            else:
                LOGGER.info("Not promoted (score %.3f vs %.3f).", new_score, old_score)

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
    LOGGER.info(
        "System stop recall=%.3f",
        best.system_metrics.get("system_stop_recall", 0.0),
    )
    LOGGER.info("Bundle written to %s", args.out)


if __name__ == "__main__":
    main()
