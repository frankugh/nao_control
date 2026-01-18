from __future__ import annotations

import os
from typing import Optional

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

    def execute(self, cmd: CommandDecision) -> None:
        label = self._normalize_label(cmd.label)
        if label == "STOP":
            self._stop_all(timeout_s=self.timeout_s)
            return
        if label == "REST":
            self._rest(timeout_s=self.timeout_s)
            return
        if label == "STAND_UP":
            self._wake_up_if_rest(timeout_s=self.timeout_s)

        behavior = self._behavior_for_command(cmd)
        if not behavior:
            raise ValueError(f"Geen behavior mapping voor label {cmd.label!r}.")
        payload = {"behavior": behavior}
        try:
            if self.api_router is not None:
                resp = self.api_router.post("/do_behavior", json=payload, timeout=self.timeout_s)
            else:
                resp = requests.post(f"{self.base_url}/do_behavior", json=payload, timeout=self.timeout_s)
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"[NAO] behavior request failed: {exc}")

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

        mapping = {
            "SITDOWN": "basic/sitdown",
            "STAND_UP": "basic/standup",
            "BOX": "greetings/dobox",
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

    def execute(self, cmd: CommandDecision) -> None:
        self._printer.execute(cmd)
        if self._executor is not None:
            self._executor.execute(cmd)
