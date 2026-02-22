import sys
from pathlib import Path
from types import ModuleType

from dialog.command_recognizer import (
    CmdRecRecognizer,
    resolve_bundle_path,
    route_decision_to_dict,
)
from dialog.interfaces import CommandDecision, RouteDecision


def test_resolve_bundle_path_latest(tmp_path: Path) -> None:
    (tmp_path / "bundle_v1").mkdir()
    (tmp_path / "bundle_v15").mkdir()
    (tmp_path / "bundle_v2").mkdir()

    resolved = resolve_bundle_path("latest", str(tmp_path))
    assert resolved.name == "bundle_v15"


def test_resolve_bundle_path_v15(tmp_path: Path) -> None:
    (tmp_path / "bundle_v15").mkdir()
    resolved = resolve_bundle_path("v15", str(tmp_path))
    assert resolved.name == "bundle_v15"


def test_resolve_bundle_path_explicit_path(tmp_path: Path) -> None:
    bundle = tmp_path / "my_bundle"
    bundle.mkdir()
    resolved = resolve_bundle_path(str(bundle), str(tmp_path))
    assert resolved == bundle.resolve()


def test_cmdrec_disabled_routes_to_dialog() -> None:
    recognizer = CmdRecRecognizer({"cmdrec": "none"})
    decision = recognizer.route("test", mode="default", active_behavior=None)
    assert decision.is_command is False
    assert decision.reason == "disabled"
    assert decision.top3 is None


def test_route_json_omits_reason_for_command() -> None:
    decision = RouteDecision(
        is_command=True,
        command=CommandDecision(label="DANCE", confidence=0.9, raw_text="dance"),
        reason=None,
        top3=[("DANCE", 0.9)],
    )
    payload = route_decision_to_dict(decision)
    assert "reason" not in payload


def test_route_json_includes_reason_for_dialog() -> None:
    decision = RouteDecision(
        is_command=False,
        command=None,
        reason="low_confidence",
        top3=[("NONE", 0.6)],
    )
    payload = route_decision_to_dict(decision)
    assert payload.get("reason")


class _FailModel:
    def predict_proba(self, _rows):
        raise AssertionError("predict_proba should not be called for preemptive STOP")


def _make_enabled_recognizer(*, model) -> CmdRecRecognizer:
    recognizer = CmdRecRecognizer({"cmdrec": "none"})
    recognizer._disabled = False
    recognizer._bundle_path = Path(".")
    recognizer._model = model
    recognizer._decision_policy = {"threshold": 0.5, "margin": 0.05}
    recognizer._labels = ["STOP", "NONE"]
    recognizer._ensure_bundle_loaded = lambda: None  # type: ignore[method-assign]
    return recognizer


def test_route_uses_cmdrec_classify_text_result(monkeypatch) -> None:
    fake_cmdrec = ModuleType("cmdrec")
    fake_predict = ModuleType("cmdrec.predict")
    fake_predict.classify_text = lambda *_a, **_k: {  # type: ignore[attr-defined]
        "top3": [{"label": "STOP", "score": 1.0}],
        "ml_decision": None,
        "final_decision": "STOP",
        "threshold": 0.5,
        "margin": 0.05,
        "stop_rule_fired": True,
        "inference_skipped": True,
        "stop_rule_mode": "preemptive",
    }
    monkeypatch.setitem(sys.modules, "cmdrec", fake_cmdrec)
    monkeypatch.setitem(sys.modules, "cmdrec.predict", fake_predict)

    recognizer = _make_enabled_recognizer(
        model=_FailModel(),
    )

    decision = recognizer.route("stop", mode="DIALOG", active_behavior=None)

    assert decision.is_command is True
    assert decision.command is not None
    assert decision.command.label == "STOP"
    assert decision.command.confidence == 1.0


def test_route_handles_none_from_cmdrec_classify_text(monkeypatch) -> None:
    fake_cmdrec = ModuleType("cmdrec")
    fake_predict = ModuleType("cmdrec.predict")
    fake_predict.classify_text = lambda *_a, **_k: {  # type: ignore[attr-defined]
        "top3": [{"label": "NONE", "score": 0.88}],
        "ml_decision": "NONE",
        "final_decision": "NONE",
        "threshold": 0.5,
        "margin": 0.05,
        "stop_rule_fired": False,
        "inference_skipped": False,
        "stop_rule_mode": "off",
    }
    monkeypatch.setitem(sys.modules, "cmdrec", fake_cmdrec)
    monkeypatch.setitem(sys.modules, "cmdrec.predict", fake_predict)

    recognizer = _make_enabled_recognizer(model=_FailModel())
    decision = recognizer.route("willekeurige tekst", mode="DIALOG", active_behavior=None)

    assert decision.is_command is False
    assert decision.reason == "low_confidence"
