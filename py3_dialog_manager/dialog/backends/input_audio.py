# py3_nao_behavior_manager/dialog/backends/input_audio.py
from __future__ import annotations

from typing import Optional

from prompt_toolkit import prompt  # required dependency

from dialog.interfaces import InputBackend, UserInput


class AudioInputBackend(InputBackend):
    """
    Audio input = mic + stt, met UX-gates.

    confirm_before_record:
        Druk Enter voordat we gaan opnemen.
    confirm_before_send:
        Na transcript: [Enter=send / e=edit / r=redo]
        (Geen cancel/skip.)
    start_timeout_s:
        None = geen timeout (praktisch oneindig, alleen vóór start spraak).
    print_input:
        Print transcript (INPUT: ...) naar console (ook als confirm uit staat).
        Als confirm aan staat, wordt INPUT sowieso getoond vóór de confirm prompt.
    status_to_console:
        Print statusregels (LISTENING/TRANSCRIBING/...) naar console.

    Vereist:
        pip install prompt_toolkit
    """

    def __init__(
        self,
        mic,
        stt,
        *,
        confirm_before_record: bool = False,
        confirm_before_send: bool = False,
        start_timeout_s: Optional[float] = 10.0,
        print_input: bool = False,
        status_to_console: bool = True,
    ) -> None:
        self.mic = mic
        self.stt = stt

        self.confirm_before_record = confirm_before_record
        self.confirm_before_send = confirm_before_send
        self.start_timeout_s = start_timeout_s
        self.print_input = print_input
        self.status_to_console = status_to_console

    def _status(self, msg: str) -> None:
        if self.status_to_console:
            print(msg)

    def _capture_and_transcribe(self) -> tuple:
        if self.confirm_before_record:
            input("Press Enter to record...")

        timeout = self.start_timeout_s
        if timeout is None:
            timeout = 10**9

        self._status("🎤 LISTENING...")
        audio = self.mic.capture_utterance(timeout_s=float(timeout))

        self._status("📝 TRANSCRIBING...")
        stt_res = self.stt.transcribe(audio)

        raw = stt_res.text or ""
        text = raw.strip()

        return audio, stt_res, raw, text

    def get_input(self) -> UserInput:
        while True:
            audio, stt_res, raw, current = self._capture_and_transcribe()

            # INPUT tonen:
            # - altijd als print_input=True
            # - óók altijd als confirm aan staat (want je wil zien wat je confirmeert)
            if self.print_input or self.confirm_before_send:
                print(f"INPUT: {current}" if current else "INPUT: <leeg>")

            if not self.confirm_before_send:
                return UserInput(raw_text=raw, text=current, audio=audio, stt=stt_res)

            # Confirm-loop: Enter / e / r
            while True:
                choice = input("Send? [Enter=send / e=edit / r=redo]: ").strip().lower()

                if choice == "":
                    return UserInput(raw_text=raw, text=current, audio=audio, stt=stt_res)

                if choice == "e":
                    # Prefilled edit
                    edited = prompt("Edit> ", default=current).strip()
                    if edited != "":
                        current = edited
                    print(f"INPUT: {current}" if current else "INPUT: <leeg>")
                    continue

                if choice == "r":
                    # redo = opnieuw opnemen + transcriben
                    break

                print("Kies: Enter / e / r")
