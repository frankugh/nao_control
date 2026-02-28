from __future__ import annotations

from py3_script_runner.client import DMClient


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def test_dm_client_uses_single_persistent_session(monkeypatch):
    calls = []
    instances = []

    class FakeSession:
        def __init__(self):
            instances.append(self)

        def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            if url.endswith("/api/script/capabilities"):
                return _Resp({"ok": True, "supports": {"say": True}})
            if url.endswith("/api/runtime_effective"):
                return _Resp({"ok": True, "runtime_config": {}})
            return _Resp({"ok": True})

    monkeypatch.setattr("py3_script_runner.client.requests.Session", FakeSession)

    client = DMClient("http://127.0.0.1:5301")
    caps = client.capabilities()
    eff = client.runtime_effective()

    assert caps["ok"] is True
    assert eff["ok"] is True
    assert len(instances) == 1
    assert len(calls) == 2


def test_dm_client_nao_set_eye_color_calls_expected_endpoint(monkeypatch):
    calls = []

    class FakeSession:
        def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            return _Resp({"ok": True})

    monkeypatch.setattr("py3_script_runner.client.requests.Session", FakeSession)

    client = DMClient("http://127.0.0.1:5301")
    out = client.nao_set_eye_color("#00ff00", duration=0.4, timeout_s=9.0)

    assert out["ok"] is True
    assert calls == [
        (
            "POST",
            "http://127.0.0.1:5301/api/nao_set_eye_color",
            {"timeout": 9.0, "json": {"color": "#00ff00", "duration": 0.4}},
        )
    ]
