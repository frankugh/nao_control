from __future__ import annotations

from typing import Any, Dict, Optional

import requests


class DMClientError(RuntimeError):
    """Raised when a DM request fails or returns an error payload."""


class DMClient:
    def __init__(self, base_url: str, timeout_s: float = 12.0) -> None:
        base = str(base_url or "").strip().rstrip("/")
        if not base:
            raise ValueError("base_url is required")
        self.base_url = base
        self.timeout_s = float(timeout_s)
        self._session = requests.Session()

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        timeout = float(timeout_s if timeout_s is not None else self.timeout_s)
        kwargs: Dict[str, Any] = {"timeout": timeout}
        if payload is not None:
            kwargs["json"] = payload
        try:
            resp = self._session.request(method=method, url=url, **kwargs)
        except requests.RequestException as exc:
            raise DMClientError(f"{method} {url} request failed: {exc}") from exc
        try:
            data = resp.json()
        except ValueError as exc:
            raise DMClientError(f"{method} {url} returned non-JSON response (status={resp.status_code})") from exc
        if resp.status_code >= 400:
            err = data.get("error") if isinstance(data, dict) else None
            raise DMClientError(f"{method} {url} failed (status={resp.status_code}): {err or data}")
        if isinstance(data, dict) and data.get("ok") is False:
            err = data.get("error") or "unknown_error"
            raise DMClientError(f"{method} {url} returned ok=false: {err}")
        if not isinstance(data, dict):
            raise DMClientError(f"{method} {url} returned invalid payload type: {type(data).__name__}")
        return data

    def capabilities(self, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        return self._request("GET", "/api/script/capabilities", timeout_s=timeout_s)

    def script_say(self, text: str, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        return self._request("POST", "/api/script/say", payload={"text": text}, timeout_s=timeout_s)

    def script_do(self, payload: Dict[str, Any], timeout_s: Optional[float] = None) -> Dict[str, Any]:
        return self._request("POST", "/api/script/do", payload=payload, timeout_s=timeout_s)

    def set_runtime_config(self, config: Dict[str, Any], timeout_s: Optional[float] = None) -> Dict[str, Any]:
        return self._request("POST", "/api/runtime_config", payload={"config": dict(config or {})}, timeout_s=timeout_s)

    def runtime_effective(self, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        return self._request("GET", "/api/runtime_effective", timeout_s=timeout_s)

    def runtime_health(self, payload: Dict[str, Any], timeout_s: Optional[float] = None) -> Dict[str, Any]:
        return self._request("POST", "/api/runtime_health", payload=dict(payload or {}), timeout_s=timeout_s)
