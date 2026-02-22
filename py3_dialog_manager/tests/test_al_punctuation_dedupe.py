from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from dialog.interfaces import LLMResult

import webapp_server


class StubLLMBackend:
    def generate(self, messages):
        return LLMResult(reply="ok", messages=list(messages))


class StubSTTBackend:
    def transcribe(self, audio):  # pragma: no cover
        raise AssertionError("STT not used in these tests")


def _load_jsonl(path: Path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def _write_jsonl(path: Path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _make_app(monkeypatch, tmp_path: Path):
    base_pipeline = SimpleNamespace(
        llm=StubLLMBackend(),
        output=None,
        status_to_console=True,
        system_prompt="SYSTEM",
        log_messages_path=None,
        log_meta={},
        max_history_turns=None,
        _behavior_executor=None,
        _cmdrec=None,
        _debug_cmdrec=False,
    )
    monkeypatch.setattr(webapp_server, "build_pipeline_from_config", lambda *_a, **_k: base_pipeline)
    monkeypatch.setattr(webapp_server, "make_stt_backend_from_config", lambda *_a, **_k: StubSTTBackend())

    fake_server_path = tmp_path / "py3_dialog_manager" / "webapp_server.py"
    fake_server_path.parent.mkdir(parents=True, exist_ok=True)
    fake_server_path.write_text("# test", encoding="utf-8")
    monkeypatch.setattr(webapp_server, "__file__", str(fake_server_path))

    al_dir = tmp_path / "py3_command_recognition_train" / "data" / "al"
    al_dir.mkdir(parents=True, exist_ok=True)

    app, _, _ = webapp_server.create_app(
        cfg={},
        config_path="<memory>",
        runtime_state_dir=str(tmp_path / "runtime"),
    )
    return app, al_dir


def test_al_review_treats_terminal_punctuation_as_duplicate(monkeypatch, tmp_path):
    app, al_dir = _make_app(monkeypatch, tmp_path)
    client = app.test_client()

    r1 = client.post("/api/al_review", json={"text": "vertel eens iets", "label": "NONE"})
    assert r1.status_code == 200
    j1 = r1.get_json()
    assert j1["ok"] is True
    assert j1["added"] is True

    r2 = client.post("/api/al_review", json={"text": "vertel eens iets.", "label": "NONE"})
    assert r2.status_code == 200
    j2 = r2.get_json()
    assert j2["ok"] is True
    assert j2["added"] is False

    reviewed = _load_jsonl(al_dir / "reviewed.jsonl")
    assert len(reviewed) == 1


def test_reviewed_endpoint_hides_legacy_punctuation_duplicates(monkeypatch, tmp_path):
    app, al_dir = _make_app(monkeypatch, tmp_path)
    client = app.test_client()

    _write_jsonl(
        al_dir / "reviewed.jsonl",
        [
            {"text": "vertel eens iets", "label": "NONE", "source": "legacy"},
            {"text": "vertel eens iets.", "label": "NONE", "source": "legacy"},
        ],
    )

    resp = client.get("/api/reviewed?offset=0&limit=20")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["total"] == 1
    assert len(data["items"]) == 1


def test_review_requeue_blocks_duplicate_with_only_punctuation_diff(monkeypatch, tmp_path):
    app, al_dir = _make_app(monkeypatch, tmp_path)
    client = app.test_client()

    _write_jsonl(
        al_dir / "reviewed.jsonl",
        [{"id": "rev1", "text": "vertel eens iets.", "label": "NONE", "source": "legacy"}],
    )
    _write_jsonl(
        al_dir / "review_queue.jsonl",
        [{"id": "q1", "text": "vertel eens iets", "suggested_label": "NONE"}],
    )

    resp = client.post(
        "/api/review_requeue",
        json={"source": "reviewed", "id": "rev1", "text": "vertel eens iets.", "label": "NONE"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["added"] is False
    assert "bestond al" in (data.get("note") or "").lower()

    queue_entries = _load_jsonl(al_dir / "review_queue.jsonl")
    assert len(queue_entries) == 1
