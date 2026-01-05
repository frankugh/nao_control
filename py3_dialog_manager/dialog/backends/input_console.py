# py3_nao_behavior_manager/dialog/backends/input_console.py
from __future__ import annotations

from prompt_toolkit import prompt  # required dependency

from dialog.interfaces import InputBackend, UserInput


class ConsoleInputBackend(InputBackend):
    """
    Console input met optionele confirm/edit.

    confirm_before_send:
        Na input: [Enter=send / e=edit / r=retype]
    print_input:
        Print INPUT naar console (ook als confirm uit staat).
        Als confirm aan staat, wordt INPUT sowieso getoond vóór de confirm prompt.

    Vereist:
        pip install prompt_toolkit
    """

    def __init__(
        self,
        prompt_text: str = "You> ",
        confirm_before_send: bool = False,
        print_input: bool = False,
        status_to_console: bool = True,
    ) -> None:
        self.prompt_text = prompt_text
        self.confirm_before_send = confirm_before_send
        self.print_input = print_input
        self.status_to_console = status_to_console

    def _status(self, msg: str) -> None:
        if self.status_to_console:
            print(msg)

    def get_input(self) -> UserInput:
        while True:
            self._status("⌨️  TYPING...")
            raw = input(self.prompt_text)
            current = (raw or "").strip()

            if self.print_input or self.confirm_before_send:
                print(f"INPUT: {current}" if current else "INPUT: <leeg>")

            if not self.confirm_before_send:
                return UserInput(raw_text=raw, text=current)

            while True:
                choice = input("Send? [Enter=send / e=edit / r=retype]: ").strip().lower()

                if choice == "":
                    return UserInput(raw_text=raw, text=current)

                if choice == "e":
                    edited = prompt("Edit> ", default=current).strip()
                    if edited != "":
                        current = edited
                    print(f"INPUT: {current}" if current else "INPUT: <leeg>")
                    continue

                if choice == "r":
                    break  # terug naar opnieuw typen

                print("Kies: Enter / e / r")
