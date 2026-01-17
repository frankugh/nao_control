from __future__ import annotations

import os
from typing import Optional

import requests

from dialog.interfaces import CommandDecision, ICommandExecutor


class BehaviorExecutor(ICommandExecutor):
    def __init__(
        self,
        base_url: Optional[str] = None,
        *,
        timeout_s: float = 5.0,
    ) -> None:
        env_url = os.environ.get("NAO_BEHAVIOR_API_URL")
        self.base_url = (base_url or env_url or "http://127.0.0.1:5001/nao").rstrip("/")
        self.timeout_s = timeout_s

    def execute(self, cmd: CommandDecision) -> None:
        behavior = self._behavior_for_command(cmd)
        if not behavior:
            raise ValueError(f"Geen behavior mapping voor label {cmd.label!r}.")
        payload = {"behavior": behavior}
        resp = requests.post(f"{self.base_url}/do_behavior", json=payload, timeout=self.timeout_s)
        resp.raise_for_status()

    def _behavior_for_command(self, cmd: CommandDecision) -> Optional[str]:
        label = cmd.label.upper().strip()
        if label == "DANCE":
            dance_key = (cmd.resolved or {}).get("dance_key") or "happy"
            return f"dances/{dance_key}"
        if label == "LOCOMOTION_REQUEST":
            action = (cmd.resolved or {}).get("locomotion", "unknown")
            if action == "exit":
                return "basic/standup"
            return "walkwithme/walkwithme"

        mapping = {
            "STOP": "basic/standup",
            "SITDOWN": "basic/sitdown",
            "STAND_UP": "basic/standup",
            "REST": "basic/tocrouched",
            "BOX": "greetings/dobox",
            "HIGH_FIVE": "greetings/dohighfive",
            "WALK_WITH_ME": "walkwithme/walkwithme",
        }
        return mapping.get(label)


class PrintBehaviorExecutor(ICommandExecutor):
    def execute(self, cmd: CommandDecision) -> None:
        resolved = cmd.resolved or {}
        print(f"EXECUTE (dry-run): {cmd.label} resolved={resolved}")
