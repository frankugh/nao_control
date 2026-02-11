from __future__ import annotations

import os
from typing import Optional, Callable

import requests

from dialog.interfaces import CommandDecision, ICommandExecutor
from dialog.nao_api_router import NaoApiRouter


class BehaviorExecutor(ICommandExecutor):
    def __init__(
        self,
        base_url: Optional[str] = None,
        *,
        timeout_s: Optional[float] = None,
        api_router: Optional[NaoApiRouter] = None,
        custom_life_enabled: bool = False,
        custom_life_settings: Optional[dict] = None,
        manage_custom_life: bool = True,
        on_finish: Optional[Callable[[CommandDecision], None]] = None,
    ) -> None:
        self.api_router = api_router
        if self.api_router is None:
            env_url = os.environ.get("NAO_BEHAVIOR_API_URL")
            self.base_url = (base_url or env_url or "http://127.0.0.1:5001/nao").rstrip("/")
        else:
            self.base_url = None
        if timeout_s is None and self.api_router is not None:
            timeout_s = self.api_router.timeout_s
        self.timeout_s = float(timeout_s or 5.0)
        self._on_finish = on_finish
        self._custom_life_enabled = bool(custom_life_enabled)
        self._custom_life_settings = (
            dict(custom_life_settings) if isinstance(custom_life_settings, dict) else None
        )
        self._manage_custom_life = bool(manage_custom_life)

    def set_custom_life_management_enabled(self, enabled: bool) -> None:
        self._manage_custom_life = bool(enabled)

    def _pause_custom_life(self) -> Optional[dict]:
        if not self._manage_custom_life:
            return None
        if not self._custom_life_enabled:
            return None
        payload = {}
        try:
            if self.api_router is not None:
                resp = self.api_router.post("/custom_life_pause", json=payload, timeout=self.timeout_s)
            else:
                resp = requests.post(f"{self.base_url}/custom_life_pause", json=payload, timeout=self.timeout_s)
            data = resp.json() if resp.ok else {}
        except Exception as exc:
            print(f"[NAO] pause custom life failed: {exc}")
            return None
        state = None
        if isinstance(data, dict):
            state = data.get("data") or data.get("state")
        return state if isinstance(state, dict) else None

    def _restore_custom_life(self, state: Optional[dict]) -> None:
        if not self._manage_custom_life:
            return
        if not self._custom_life_enabled:
            return
        if self._custom_life_settings is not None:
            payload = {"settings": self._custom_life_settings}
            path = "/custom_life_apply"
        else:
            if not state:
                return
            payload = {"state": state}
            path = "/custom_life_resume"
        try:
            if self.api_router is not None:
                self.api_router.post(path, json=payload, timeout=self.timeout_s)
            else:
                requests.post(f"{self.base_url}{path}", json=payload, timeout=self.timeout_s)
        except Exception as exc:
            print(f"[NAO] restore custom life failed: {exc}")

    def set_on_finish(self, on_finish: Optional[Callable[[CommandDecision], None]]) -> None:
        self._on_finish = on_finish

    def _notify_finish(self, cmd: CommandDecision) -> None:
        if self._on_finish is None:
            return
        try:
            self._on_finish(cmd)
        except Exception as exc:
            print(f"[NAO] on_finish callback failed: {exc}")

    def execute(self, cmd: CommandDecision) -> None:
        label = self._normalize_label(cmd.label)
        if label == "STOP":
            self._stop_all(timeout_s=self.timeout_s)
            self._notify_finish(cmd)
            return
        if label == "REST":
            self._rest(timeout_s=self.timeout_s)
            self._notify_finish(cmd)
            return
        if label == "STAND_UP":
            self._wake_up_if_rest(timeout_s=self.timeout_s)

        behavior = self._behavior_for_command(cmd)
        if not behavior:
            raise ValueError(f"Geen behavior mapping voor label {cmd.label!r}.")
        payload = {"behavior": behavior}
        pause_state = self._pause_custom_life()
        try:
            if self.api_router is not None:
                resp = self.api_router.post("/do_behavior", json=payload, timeout=self.timeout_s)
            else:
                resp = requests.post(f"{self.base_url}/do_behavior", json=payload, timeout=self.timeout_s)
            resp.raise_for_status()
            # Log non-ok responses from Py2 (it returns 200 with status=warning/error).
            try:
                data = resp.json()
            except ValueError:
                data = None
            if isinstance(data, dict) and data.get("status") not in (None, "ok"):
                print(f"[NAO] behavior not ok: {data} payload={payload}")
        except requests.RequestException as exc:
            print(f"[NAO] behavior request failed: {exc}")
        finally:
            self._restore_custom_life(pause_state)
        self._notify_finish(cmd)

    def _normalize_label(self, label: str) -> str:
        return label.upper().strip().replace("\\_", "_")

    def _stop_all(self, *, timeout_s: float) -> None:
        payload = {}
        try:
            if self.api_router is not None:
                self.api_router.post("/stop_move", json=payload, timeout=timeout_s)
                self.api_router.post("/stop_all_behaviors", json=payload, timeout=timeout_s)
            else:
                requests.post(f"{self.base_url}/stop_move", json=payload, timeout=timeout_s)
                requests.post(f"{self.base_url}/stop_all_behaviors", json=payload, timeout=timeout_s)
        except requests.RequestException as exc:
            print(f"[NAO] stop request failed: {exc}")

    def _wake_up_if_rest(self, *, timeout_s: float) -> None:
        payload = {}
        try:
            if self.api_router is not None:
                resp = self.api_router.get("/is_awake", timeout=timeout_s)
            else:
                resp = requests.get(f"{self.base_url}/is_awake", timeout=timeout_s)
            data = resp.json() if resp.ok else {}
            is_awake = bool((data.get("data") or {}).get("is_awake"))
        except Exception as exc:
            print(f"[NAO] is_awake failed: {exc}")
            return

        if is_awake:
            return

        try:
            if self.api_router is not None:
                self.api_router.post("/wake_up", json=payload, timeout=timeout_s)
            else:
                requests.post(f"{self.base_url}/wake_up", json=payload, timeout=timeout_s)
        except requests.RequestException as exc:
            print(f"[NAO] wake_up failed: {exc}")

    def _rest(self, *, timeout_s: float) -> None:
        payload = {}
        try:
            if self.api_router is not None:
                self.api_router.post("/rest", json=payload, timeout=timeout_s)
            else:
                requests.post(f"{self.base_url}/rest", json=payload, timeout=timeout_s)
        except requests.RequestException as exc:
            print(f"[NAO] rest request failed: {exc}")

    def _get_posture(self) -> Optional[str]:
        try:
            if self.api_router is not None:
                resp = self.api_router.get("/posture", timeout=self.timeout_s)
            else:
                resp = requests.get(f"{self.base_url}/posture", timeout=self.timeout_s)
            data = resp.json() if resp.ok else {}
        except Exception as exc:
            print(f"[NAO] posture request failed: {exc}")
            return None

        payload = data.get("data") or {}
        if isinstance(payload, dict):
            posture = payload.get("posture")
            if isinstance(posture, str) and posture.strip():
                return posture.strip()
            result = payload.get("result")
            if isinstance(result, str) and result.strip():
                return result.strip()
        return None

    def _behavior_for_command(self, cmd: CommandDecision) -> Optional[str]:
        label = self._normalize_label(cmd.label)
        if label == "DANCE":
            resolved = cmd.resolved or {}
            dance_behavior = resolved.get("dance_behavior")
            if isinstance(dance_behavior, str) and dance_behavior.strip():
                return dance_behavior.strip()
            dance_key = resolved.get("dance_key") or "happy"
            return f"dances/{dance_key}"
        if label == "LOCOMOTION_REQUEST":
            action = (cmd.resolved or {}).get("locomotion", "unknown")
            if action == "exit":
                return "basic/standup"
            return "walkwithme/walkwithme"
        if label == "BOX":
            posture = self._get_posture()
            if posture:
                posture_l = posture.lower()
                if posture_l.startswith("sit"):
                    return "greetings/doboxsit"
                if posture_l.startswith("stand"):
                    return "greetings"

        mapping = {
            "SITDOWN": "basic/sitdown",
            "STAND_UP": "basic/standup",
            "HIGH_FIVE": "greetings/dohighfive",
            "WALK_WITH_ME": "walkwithme/walkwithme",
            "WAVE": "animations/Stand/Emotions/Neutral/Hello_1",
        }
        return mapping.get(label)


class PrintBehaviorExecutor(ICommandExecutor):
    def execute(self, cmd: CommandDecision) -> None:
        resolved = cmd.resolved or {}
        print(f"EXECUTE (dry-run): {cmd.label} resolved={resolved}")


class ConsoleAndBehaviorExecutor(ICommandExecutor):
    def __init__(self, executor: ICommandExecutor | None) -> None:
        self._printer = PrintBehaviorExecutor()
        self._executor = executor
        self._on_finish: Optional[Callable[[CommandDecision], None]] = None

    def set_on_finish(self, on_finish: Optional[Callable[[CommandDecision], None]]) -> None:
        self._on_finish = on_finish

    def set_custom_life_management_enabled(self, enabled: bool) -> None:
        if self._executor is None:
            return
        setter = getattr(self._executor, "set_custom_life_management_enabled", None)
        if callable(setter):
            setter(enabled)

    def execute(self, cmd: CommandDecision) -> None:
        self._printer.execute(cmd)
        if self._executor is not None:
            self._executor.execute(cmd)
        if self._on_finish is not None:
            try:
                self._on_finish(cmd)
            except Exception as exc:
                print(f"[NAO] on_finish callback failed: {exc}")
