from __future__ import annotations

import time
from typing import Iterable, Optional

import requests


class NaoApiRouter:
    _NO_FALLBACK_PATHS = {"/tts", "/play_stream", "/play_audio"}

    def __init__(
        self,
        *,
        primary_base_url: str,
        fallback_base_url: str,
        health_ttl_s: float = 30.0,
        health_checks: Optional[Iterable[str]] = None,
        timeout_s: float = 3.0,
        status_to_console: bool = True,
    ) -> None:
        if not primary_base_url or not isinstance(primary_base_url, str):
            raise ValueError("primary_base_url moet een string zijn.")
        if not fallback_base_url or not isinstance(fallback_base_url, str):
            raise ValueError("fallback_base_url moet een string zijn.")

        self.primary_base_url = primary_base_url.rstrip("/")
        self.fallback_base_url = fallback_base_url.rstrip("/")
        self.health_ttl_s = float(health_ttl_s)
        self.timeout_s = float(timeout_s)
        self.status_to_console = bool(status_to_console)

        checks = list(health_checks or [])
        for check in checks:
            if check not in ("py3_ping", "py3_nao_ping"):
                raise ValueError("health_checks mag alleen 'py3_ping' en/of 'py3_nao_ping' bevatten.")
        self.health_checks = tuple(checks)

        self._primary_ok: Optional[bool] = None
        self._last_check: Optional[float] = None

    def _status(self, msg: str) -> None:
        if self.status_to_console:
            print(msg)

    def _same_endpoint_retry(self) -> bool:
        return self.primary_base_url == self.fallback_base_url

    def _fallback_message(self, template_fallback: str, template_same_endpoint: str) -> str:
        if self._same_endpoint_retry():
            return template_same_endpoint
        return template_fallback

    def _primary_root(self) -> str:
        if self.primary_base_url.endswith("/nao"):
            return self.primary_base_url[:-4]
        return self.primary_base_url

    def _ping_url(self, check: str) -> str:
        if check == "py3_ping":
            return self._primary_root().rstrip("/") + "/ping"
        if check == "py3_nao_ping":
            return self.primary_base_url.rstrip("/") + "/ping"
        raise ValueError("Onbekende health check.")

    def _ping(self, url: str, *, timeout: float) -> bool:
        try:
            resp = requests.get(url, timeout=timeout)
        except requests.RequestException:
            return False
        if resp.status_code >= 400:
            return False
        try:
            data = resp.json()
        except ValueError:
            return True
        return data.get("status") == "ok"

    def _ping_primary(self) -> bool:
        if not self.health_checks:
            return True
        timeout = self.timeout_s
        for check in self.health_checks:
            url = self._ping_url(check)
            if not self._ping(url, timeout=timeout):
                return False
        return True

    def check_primary(self, *, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and self._last_check is not None:
            if (now - self._last_check) < self.health_ttl_s:
                return bool(self._primary_ok)

        ok = self._ping_primary()
        prev = self._primary_ok
        self._primary_ok = ok
        self._last_check = now

        if prev is None:
            self._status("[NAO] primary " + ("OK" if ok else "DOWN"))
        elif prev != ok:
            self._status("[NAO] primary " + ("OK" if ok else "DOWN"))

        return ok

    def _request(self, base_url: str, method: str, path: str, *, timeout: float, **kwargs):
        url = base_url + (path if path.startswith("/") else "/" + path)
        return requests.request(method, url, timeout=timeout, **kwargs)

    def request(self, method: str, path: str, **kwargs):
        timeout = kwargs.pop("timeout", self.timeout_s)
        no_fallback = path in self._NO_FALLBACK_PATHS
        primary_ok = self.check_primary() if self.health_checks else True

        if primary_ok:
            try:
                resp = self._request(self.primary_base_url, method, path, timeout=timeout, **kwargs)
                if resp.status_code >= 500:
                    self._status(
                        self._fallback_message(
                            "[NAO] primary returned %s; using fallback",
                            "[NAO] primary returned %s; retrying same endpoint",
                        )
                        % resp.status_code
                    )
                    self._primary_ok = False
                    self._last_check = time.monotonic()
                    if no_fallback:
                        return resp
                else:
                    return resp
            except requests.RequestException as exc:
                self._status(
                    self._fallback_message(
                        "[NAO] primary request failed; using fallback: %s",
                        "[NAO] primary request failed; retrying same endpoint: %s",
                    )
                    % exc
                )
                self._primary_ok = False
                self._last_check = time.monotonic()
                if no_fallback:
                    raise

        return self._request(self.fallback_base_url, method, path, timeout=timeout, **kwargs)

    def get(self, path: str, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs):
        return self.request("POST", path, **kwargs)
