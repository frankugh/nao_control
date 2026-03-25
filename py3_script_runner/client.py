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
            if isinstance(data, dict):
                detail = str(data.get("detail") or "").strip()
                if detail:
                    err = f"{err}: {detail}" if err else detail
            raise DMClientError(f"{method} {url} failed (status={resp.status_code}): {err or data}")
        if isinstance(data, dict) and data.get("ok") is False:
            err = data.get("error") or "unknown_error"
            raise DMClientError(f"{method} {url} returned ok=false: {err}")
        if not isinstance(data, dict):
            raise DMClientError(f"{method} {url} returned invalid payload type: {type(data).__name__}")
        return data

    def _request_bytes(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Dict[str, Any]] = None,
        timeout_s: Optional[float] = None,
        accept: str = "audio/wav",
    ) -> bytes:
        url = f"{self.base_url}{path}"
        timeout = float(timeout_s if timeout_s is not None else self.timeout_s)
        kwargs: Dict[str, Any] = {"timeout": timeout, "headers": {"Accept": accept}}
        if payload is not None:
            kwargs["json"] = payload
        try:
            resp = self._session.request(method=method, url=url, **kwargs)
        except requests.RequestException as exc:
            raise DMClientError(f"{method} {url} request failed: {exc}") from exc
        if resp.status_code >= 400:
            err: Optional[str] = None
            try:
                data = resp.json()
            except ValueError:
                data = None
            if isinstance(data, dict):
                err = str(data.get("error") or "").strip() or None
                detail = str(data.get("detail") or "").strip()
                if detail:
                    err = f"{err}: {detail}" if err else detail
            if not err:
                err = (resp.text or "").strip() or f"status={resp.status_code}"
            raise DMClientError(f"{method} {url} failed (status={resp.status_code}): {err}")
        return bytes(resp.content or b"")

    def capabilities(self, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        return self._request("GET", "/api/script/capabilities", timeout_s=timeout_s)

    def summary_page_url(self) -> str:
        return f"{self.base_url}/summary"

    def summary_get(self, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        return self._request("GET", "/api/summary", timeout_s=timeout_s)

    def summary_start(self, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        return self._request("POST", "/api/summary/start", payload={}, timeout_s=timeout_s)

    def summary_abort(self, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        return self._request("POST", "/api/summary/abort", payload={}, timeout_s=timeout_s)

    def script_say(
        self,
        text: str,
        *,
        timeout_s: Optional[float] = None,
        preloaded_audio_b64: Optional[str] = None,
        preloaded_audio_format: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"text": text}
        if preloaded_audio_b64:
            payload["preloaded_audio_b64"] = str(preloaded_audio_b64)
            payload["preloaded_audio_format"] = str(preloaded_audio_format or "wav").strip().lower() or "wav"
        return self._request("POST", "/api/script/say", payload=payload, timeout_s=timeout_s)

    def script_do(self, payload: Dict[str, Any], timeout_s: Optional[float] = None) -> Dict[str, Any]:
        return self._request("POST", "/api/script/do", payload=payload, timeout_s=timeout_s)

    def script_tts_profile(
        self,
        *,
        timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        return self._request("POST", "/api/script/tts_profile", payload={}, timeout_s=timeout_s)

    def script_tts_render(
        self,
        *,
        text: str,
        timeout_s: Optional[float] = None,
    ) -> bytes:
        payload: Dict[str, Any] = {"text": text}
        return self._request_bytes("POST", "/api/script/tts_render", payload=payload, timeout_s=timeout_s)

    def nao_set_eye_color(
        self,
        color: str,
        *,
        duration: Optional[float] = None,
        timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"color": str(color or "").strip()}
        if duration is not None:
            payload["duration"] = float(duration)
        return self._request("POST", "/api/nao_set_eye_color", payload=payload, timeout_s=timeout_s)

    def runtime_effective(self, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        return self._request("GET", "/api/runtime_effective", timeout_s=timeout_s)

    def runtime_health(self, payload: Dict[str, Any], timeout_s: Optional[float] = None) -> Dict[str, Any]:
        return self._request("POST", "/api/runtime_health", payload=dict(payload or {}), timeout_s=timeout_s)

    def nao_command_state(self, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        return self._request("GET", "/api/nao_command_state", timeout_s=timeout_s)

    def auto_rest_suspend_acquire(
        self,
        *,
        lease_id: str,
        owner: str,
        reason: str,
        ttl_s: float,
        timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        return self._request(
            "POST",
            "/api/auto_rest_suspend/acquire",
            payload={
                "lease_id": str(lease_id or "").strip(),
                "owner": str(owner or "").strip(),
                "reason": str(reason or "").strip(),
                "ttl_s": float(ttl_s),
            },
            timeout_s=timeout_s,
        )

    def auto_rest_suspend_renew(
        self,
        *,
        lease_id: str,
        owner: str,
        ttl_s: float,
        timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        return self._request(
            "POST",
            "/api/auto_rest_suspend/renew",
            payload={
                "lease_id": str(lease_id or "").strip(),
                "owner": str(owner or "").strip(),
                "ttl_s": float(ttl_s),
            },
            timeout_s=timeout_s,
        )

    def auto_rest_suspend_release(
        self,
        *,
        lease_id: str,
        timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        return self._request(
            "POST",
            "/api/auto_rest_suspend/release",
            payload={"lease_id": str(lease_id or "").strip()},
            timeout_s=timeout_s,
        )
