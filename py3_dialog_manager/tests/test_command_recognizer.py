from pathlib import Path

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
