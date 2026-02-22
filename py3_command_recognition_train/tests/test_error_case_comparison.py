from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

pytest.importorskip("joblib")
pytest.importorskip("sklearn")

from cmdrec.train import build_error_summary, build_system_disagreement_cases


def test_system_disagreement_cases_include_only_diff_and_outcome() -> None:
    records = [
        {
            "text": "zelfde keuze",
            "true_label": "NONE",
            "ml_pred_label": "NONE",
            "system_pred_label": "NONE",
            "stop_rule_hit": False,
        },
        {
            "text": "system fixt ml",
            "true_label": "STOP",
            "ml_pred_label": "NONE",
            "system_pred_label": "STOP",
            "stop_rule_hit": True,
        },
        {
            "text": "system maakt slechter",
            "true_label": "NONE",
            "ml_pred_label": "NONE",
            "system_pred_label": "STOP",
            "stop_rule_hit": True,
        },
        {
            "text": "beide fout, andere keuze",
            "true_label": "BOX",
            "ml_pred_label": "DANCE",
            "system_pred_label": "STOP",
            "stop_rule_hit": True,
        },
    ]

    out = build_system_disagreement_cases(records)
    assert [item["text"] for item in out] == [
        "system fixt ml",
        "system maakt slechter",
        "beide fout, andere keuze",
    ]
    assert out[0]["comparison_outcome"] == "system_fixed_ml_error"
    assert out[0]["comparison_reason"] == "stop_rule_override"
    assert out[0]["ml_correct"] is False
    assert out[0]["system_correct"] is True
    assert out[1]["comparison_outcome"] == "system_overrode_ml_correct"
    assert out[2]["comparison_outcome"] == "both_wrong_different_choice"


def test_error_summary_contains_disagreement_breakdown() -> None:
    y_true = ["STOP", "NONE", "BOX", "DANCE"]
    ml_preds = ["NONE", "NONE", "DANCE", "DANCE"]
    system_preds = ["STOP", "STOP", "STOP", "DANCE"]

    summary = build_error_summary(y_true, ml_preds, system_preds)
    assert summary["system_ml_disagreement_count"] == 3
    assert summary["system_ml_disagreement_breakdown"] == {
        "system_better": 1,
        "ml_better": 1,
        "both_wrong": 1,
    }
