from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")
from cmdrec.predict import classify_text


class _FailModel:
    def predict_proba(self, _rows):
        raise AssertionError("predict_proba should not be called when stop fast-path fires")


class _StubModel:
    def __init__(self, row: list[float]):
        self._row = np.asarray([row], dtype=float)
        self.called = 0

    def predict_proba(self, _rows):
        self.called += 1
        return self._row


def test_classify_text_preemptive_mode_skips_inference() -> None:
    policy = {"threshold": 0.5, "margin": 0.05, "stop_rule_mode": "preemptive"}
    result = classify_text(_FailModel(), policy, ["STOP", "NONE"], "stop")

    assert result["inference_skipped"] is True
    assert result["stop_rule_fired"] is True
    assert result["final_decision"] == "STOP"
    assert result["ml_decision"] is None


def test_classify_text_off_mode_uses_model_path() -> None:
    policy = {"threshold": 0.5, "margin": 0.05, "stop_rule_mode": "off"}
    model = _StubModel([0.1, 0.9])  # STOP low, NONE high
    result = classify_text(model, policy, ["STOP", "NONE"], "stop")

    assert model.called == 1
    assert result["inference_skipped"] is False
    assert result["stop_rule_fired"] is False
    assert result["ml_decision"] == "NONE"
    assert result["final_decision"] == "NONE"
