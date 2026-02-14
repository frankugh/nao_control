from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional
import sys
import time

from .client import DMClient
from .ppt_controller import ComPptController, PPTControllerError, PptControllerProtocol


ClientFactory = Callable[[str, float], DMClient]
InputFunc = Callable[[str], str]
SleepFunc = Callable[[float], None]


class _RunAbortRequested(RuntimeError):
    pass


@dataclass
class RunResult:
    completed_steps: int
    total_steps: int
    aborted: bool
    log_path: Path


class ScriptRunner:
    def __init__(
        self,
        script: Dict[str, Any],
        *,
        client_factory: Optional[ClientFactory] = None,
        input_func: Optional[InputFunc] = None,
        sleep_func: Optional[SleepFunc] = None,
        log_dir: Optional[Path] = None,
        ppt_controller: Optional[PptControllerProtocol] = None,
    ) -> None:
        self.script = script
        self.client_factory = client_factory or (lambda url, timeout_s: DMClient(url, timeout_s=timeout_s))
        self.input_func = input_func or input
        self.sleep_func = sleep_func or time.sleep
        self.defaults = dict(script.get("defaults") or {})
        self.ppt_cfg = dict(script.get("ppt") or {})
        self.ppt_enabled = bool(self.ppt_cfg.get("enabled", False))
        self._ppt_controller = ppt_controller
        self._ppt_prepared = False
        self._capture_on = True
        self._last_script_ppt_position: Optional[Dict[str, int]] = None

        self.log_dir = Path(log_dir) if log_dir is not None else (Path(__file__).resolve().parent / "logs")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_path = self.log_dir / f"run_{stamp}.log"
        self._log_handle = self.log_path.open("a", encoding="utf-8")

        self.clients: Dict[str, DMClient] = {}
        self.robot_cfgs: Dict[str, Dict[str, Any]] = {}
        self._runtime_cfg_applied: Dict[str, bool] = {}
        self.readiness_poll_interval_s = self._parse_positive_float(
            self.defaults.get("readiness_poll_interval_s", 3.0),
            default=3.0,
        )
        self.readiness_timeout_s = self._parse_nonnegative_float(
            self.defaults.get("readiness_timeout_s", 0.0),
            default=0.0,
        )
        self.readiness_request_timeout_s = self._parse_positive_float(
            self.defaults.get("readiness_request_timeout_s", 3.0),
            default=3.0,
        )
        self._build_clients()

    @staticmethod
    def _parse_positive_float(value: Any, *, default: float) -> float:
        try:
            parsed = float(value)
        except Exception:
            return float(default)
        if parsed <= 0:
            return float(default)
        return float(parsed)

    @staticmethod
    def _parse_nonnegative_float(value: Any, *, default: float) -> float:
        try:
            parsed = float(value)
        except Exception:
            return float(default)
        if parsed < 0:
            return float(default)
        return float(parsed)

    @staticmethod
    def _normalize_position(raw: Dict[str, Any]) -> Dict[str, int]:
        slide = max(1, int(raw.get("slide", 1)))
        build = max(0, int(raw.get("build", 0)))
        return {"slide": slide, "build": build}

    @staticmethod
    def _format_position(pos: Dict[str, int]) -> str:
        return f"slide={int(pos.get('slide', 0))} build={int(pos.get('build', 0))}"

    def _build_clients(self) -> None:
        timeout_s = float(self.defaults.get("request_timeout_s", 12))
        for robot_id, cfg in (self.script.get("robots") or {}).items():
            robot_cfg = dict(cfg or {})
            self.robot_cfgs[robot_id] = robot_cfg
            dm_url = str(robot_cfg.get("dm_url") or "").strip()
            self.clients[robot_id] = self.client_factory(dm_url, timeout_s)

    def _log(self, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {message}"
        print(line)
        self._log_handle.write(line + "\n")
        self._log_handle.flush()

    def _runtime_health_payload(self, runtime_cfg: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "nao_ip": runtime_cfg.get("nao_ip"),
            "nao_ip_enabled": bool(runtime_cfg.get("nao_ip_enabled", False)),
            "nao_base_url": runtime_cfg.get("nao_base_url"),
            "behavior_manager_url": runtime_cfg.get("behavior_manager_url"),
            "base_enabled": bool(runtime_cfg.get("base_enabled", True)),
            "behavior_enabled": bool(runtime_cfg.get("behavior_enabled", True)),
        }

    def _health_issues(self, runtime_cfg: Dict[str, Any], health: Dict[str, Any]) -> list[str]:
        issues: list[str] = []
        base_enabled = bool(runtime_cfg.get("base_enabled", True))
        behavior_enabled = bool(runtime_cfg.get("behavior_enabled", True))
        nao_ip_enabled = bool(runtime_cfg.get("nao_ip_enabled", False))

        base = health.get("base") if isinstance(health, dict) else {}
        behavior = health.get("behavior") if isinstance(health, dict) else {}
        nao = health.get("nao") if isinstance(health, dict) else {}

        base_ping = bool((base or {}).get("ping"))
        base_nao_ping = bool((base or {}).get("nao_ping"))
        behavior_ping = bool((behavior or {}).get("ping"))
        behavior_nao_ping = bool((behavior or {}).get("nao_ping"))
        nao_ping = bool((nao or {}).get("ping"))

        if nao_ip_enabled and not nao_ping:
            issues.append("nao tcp down")

        if base_enabled:
            if not base_ping:
                issues.append("base connector down")
            elif not base_nao_ping:
                issues.append("base->NAO down")

        if behavior_enabled:
            if not behavior_ping:
                issues.append("behavior manager down")
            elif not behavior_nao_ping:
                issues.append("behavior->NAO down")

        return issues

    def _check_robot_ready(self, robot_id: str) -> tuple[bool, str, Dict[str, Any]]:
        client = self.clients[robot_id]
        timeout_s = self.readiness_request_timeout_s
        robot_cfg = self.robot_cfgs.get(robot_id) or {}
        runtime_override = robot_cfg.get("runtime_config")

        try:
            caps = client.capabilities(timeout_s=timeout_s)
        except Exception as exc:
            return False, f"DM down ({exc})", {}
        supports = caps.get("supports", {}) if isinstance(caps, dict) else {}

        if isinstance(runtime_override, dict) and runtime_override and not self._runtime_cfg_applied.get(robot_id):
            try:
                self._log(f"Robot {robot_id}: applying runtime config overrides")
                client.set_runtime_config(runtime_override, timeout_s=timeout_s)
                self._runtime_cfg_applied[robot_id] = True
            except Exception as exc:
                return False, f"runtime config apply failed ({exc})", {"supports": supports}

        try:
            effective = client.runtime_effective(timeout_s=timeout_s)
            runtime_cfg = effective.get("runtime_config", {}) if isinstance(effective, dict) else {}
            if not isinstance(runtime_cfg, dict):
                runtime_cfg = {}
        except Exception as exc:
            return False, f"runtime_effective unavailable ({exc})", {"supports": supports}

        try:
            health = client.runtime_health(self._runtime_health_payload(runtime_cfg), timeout_s=timeout_s)
        except Exception as exc:
            return False, f"runtime_health failed ({exc})", {"supports": supports, "runtime_config": runtime_cfg}

        issues = self._health_issues(runtime_cfg, health)
        if issues:
            return False, "; ".join(issues), {"supports": supports, "runtime_config": runtime_cfg}
        return True, "dm+connections OK", {"supports": supports, "runtime_config": runtime_cfg}

    def _wait_for_readiness(self) -> Dict[str, Dict[str, Any]]:
        total = len(self.clients)
        started = time.monotonic()
        last_lines: Dict[str, str] = {}

        self._log(
            "Readiness: waiting for {n} robot(s) (DM + verbindingen), polling every {poll:.1f}s".format(
                n=total,
                poll=self.readiness_poll_interval_s,
            )
        )

        while True:
            ready_info: Dict[str, Dict[str, Any]] = {}
            ready_count = 0

            for robot_id in sorted(self.clients.keys()):
                ready, status, info = self._check_robot_ready(robot_id)
                line = "Robot {rid}: {state} - {status}".format(
                    rid=robot_id,
                    state="READY" if ready else "WAIT",
                    status=status,
                )
                if last_lines.get(robot_id) != line:
                    self._log(line)
                    last_lines[robot_id] = line
                if ready:
                    ready_count += 1
                    ready_info[robot_id] = info

            if ready_count == total:
                self._log("Readiness complete: all robots ready.")
                return ready_info

            elapsed = time.monotonic() - started
            timeout_s = self.readiness_timeout_s
            if timeout_s > 0 and elapsed >= timeout_s:
                waiting = [rid for rid in sorted(self.clients.keys()) if rid not in ready_info]
                raise RuntimeError(
                    "Readiness timeout after {timeout:.1f}s; still waiting for: {robots}".format(
                        timeout=timeout_s,
                        robots=", ".join(waiting) or "unknown",
                    )
                )

            if timeout_s > 0:
                remaining = max(0.0, timeout_s - elapsed)
                self._log(
                    "Readiness: {ready}/{total} ready. Retry in {poll:.1f}s (timeout in {remaining:.0f}s).".format(
                        ready=ready_count,
                        total=total,
                        poll=self.readiness_poll_interval_s,
                        remaining=remaining,
                    )
                )
            else:
                self._log(
                    "Readiness: {ready}/{total} ready. Retry in {poll:.1f}s.".format(
                        ready=ready_count,
                        total=total,
                        poll=self.readiness_poll_interval_s,
                    )
                )
            self.sleep_func(self.readiness_poll_interval_s)

    def _get_ppt_controller(self) -> PptControllerProtocol:
        if not self.ppt_enabled:
            raise RuntimeError("PPT is not enabled for this script.")
        if self._ppt_controller is None:
            self._ppt_controller = ComPptController()
        return self._ppt_controller

    def _get_ppt_position(self) -> Dict[str, int]:
        controller = self._get_ppt_controller()
        try:
            raw = controller.get_position()
        except PPTControllerError as exc:
            raise RuntimeError(str(exc)) from exc
        return self._normalize_position(raw)

    def _prepare_ppt_if_needed(self) -> None:
        if not self.ppt_enabled or self._ppt_prepared:
            return
        if sys.platform != "win32":
            raise RuntimeError("PPT feature requires Windows + PowerPoint COM")

        ppt_file = str(self.ppt_cfg.get("file") or "").strip()
        if not ppt_file:
            raise RuntimeError("ppt.enabled=true requires ppt.file")

        fullscreen_required = bool(self.ppt_cfg.get("fullscreen_required", True))
        self._log(f"[PPT] opening slideshow: {ppt_file}")

        controller = self._get_ppt_controller()
        try:
            controller.open_and_start_slideshow(ppt_file, fullscreen_required=fullscreen_required)
        except PPTControllerError as exc:
            raise RuntimeError(str(exc)) from exc

        if fullscreen_required and not controller.is_fullscreen_slideshow():
            raise RuntimeError("PowerPoint slideshow is not fullscreen.")

        pos = self._get_ppt_position()
        self._last_script_ppt_position = dict(pos)
        self._log(f"[PPT] {self._format_position(pos)}")

        self._capture_on = bool(self.ppt_cfg.get("start_capture_on_run", True))
        self._log("[CAPTURE] ON" if self._capture_on else "[CAPTURE] OFF")
        self._ppt_prepared = True

    def _operator_next_build(self) -> None:
        controller = self._get_ppt_controller()
        try:
            controller.next_build()
        except PPTControllerError as exc:
            raise RuntimeError(str(exc)) from exc
        pos = self._get_ppt_position()
        self._log(f"[PPT] operator next -> {self._format_position(pos)}")

    def _operator_prev_build(self) -> None:
        controller = self._get_ppt_controller()
        try:
            controller.prev_build()
        except PPTControllerError as exc:
            raise RuntimeError(str(exc)) from exc
        pos = self._get_ppt_position()
        self._log(f"[PPT] operator prev -> {self._format_position(pos)}")

    def _snapback_to_script_anchor(self) -> None:
        if not self.ppt_enabled:
            return
        if not self._last_script_ppt_position:
            self._log("[PPT] snapback skipped: no script anchor yet")
            return

        target = dict(self._last_script_ppt_position)
        controller = self._get_ppt_controller()
        self._log(f"[PPT] snapback to {self._format_position(target)}")
        try:
            controller.goto(target["slide"], target.get("build"))
        except PPTControllerError as exc:
            raise RuntimeError(str(exc)) from exc

        pos = self._get_ppt_position()
        self._last_script_ppt_position = dict(pos)
        self._log(f"[PPT] {self._format_position(pos)}")

    def _read_control_input(self, prompt: str = "") -> str:
        try:
            return str(self.input_func(prompt) or "").strip().lower()
        except EOFError as exc:
            raise RuntimeError("manual step requires interactive stdin (capture/manual controls)") from exc

    def _ask_quit_action(self) -> str:
        while True:
            choice = self._read_control_input("Quit run? [a]bort / [c]ontinue: ")
            if choice in {"a", "abort"}:
                return "abort"
            if choice in {"c", "continue", "", "n", "no"}:
                return "continue"

    def _toggle_capture(self) -> None:
        if not self.ppt_enabled:
            self._log("[CAPTURE] toggle ignored: PPT not enabled")
            return

        if self._capture_on:
            self._capture_on = False
            self._log("[CAPTURE] OFF")
            return

        self._capture_on = True
        self._log("[CAPTURE] ON")
        self._snapback_to_script_anchor()

    def _pause_while_capture_off(self) -> None:
        if not self.ppt_enabled:
            return

        while not self._capture_on:
            self._log("[CAPTURE] paused (ENTER=next_build, p=prev_build, c=capture ON, q=quit)")
            choice = self._read_control_input("")
            if choice == "":
                self._operator_next_build()
                continue
            if choice in {"p", "prev", "previous"}:
                self._operator_prev_build()
                continue
            if choice in {"c", "capture"}:
                self._capture_on = True
                self._log("[CAPTURE] ON")
                self._snapback_to_script_anchor()
                continue
            if choice in {"q", "quit"}:
                if self._ask_quit_action() == "abort":
                    raise _RunAbortRequested("Abort requested while capture was paused.")
                continue
            self._log("[CAPTURE] unknown key. Use ENTER, p, c, or q.")

    def _check_capture_sync(self) -> None:
        if not self.ppt_enabled or not self._capture_on or not self._last_script_ppt_position:
            return

        current = self._get_ppt_position()
        expected = dict(self._last_script_ppt_position)
        if current == expected:
            return

        self._log(
            "[SYNC] mismatch detected -> hard pause (expected {exp}, got {got})".format(
                exp=self._format_position(expected),
                got=self._format_position(current),
            )
        )
        self._capture_on = False
        self._log("[CAPTURE] OFF")
        self._pause_while_capture_off()

    def preflight(self) -> None:
        self._log("Preflight: checking robot DM capabilities...")
        ready_info = self._wait_for_readiness()
        for robot_id in sorted(self.clients.keys()):
            info = ready_info.get(robot_id) or {}
            supports = info.get("supports", {}) if isinstance(info, dict) else {}
            runtime = info.get("runtime_config", {}) if isinstance(info, dict) else {}
            self._log(f"Robot {robot_id}: capabilities OK ({supports})")
            self._log(
                "Robot {rid}: runtime nao_ip={ip} base={base} out={out}/{tts}".format(
                    rid=robot_id,
                    ip=runtime.get("nao_ip"),
                    base=runtime.get("nao_base_url"),
                    out=runtime.get("output_target"),
                    tts=runtime.get("tts_engine"),
                )
            )

        self._prepare_ppt_if_needed()
        self._log("Preflight complete.")

    def _wait_for_step_start(self, step: Dict[str, Any], index: int, total: int) -> None:
        start = step.get("start") or {}
        mode = str(start.get("mode") or "").strip().lower()
        step_id = step.get("id", f"step_{index+1}")

        if mode == "manual":
            self._log(f"[{index + 1}/{total}] {step_id}: waiting for ENTER (manual start)")
            while True:
                choice = self._read_control_input("")
                if choice == "":
                    return
                if choice in {"c", "capture"}:
                    self._toggle_capture()
                    self._pause_while_capture_off()
                    continue
                if choice in {"q", "quit"}:
                    if self._ask_quit_action() == "abort":
                        raise _RunAbortRequested(f"[{index + 1}/{total}] {step_id}: abort requested")
                    continue
                self._log(f"[{index + 1}/{total}] {step_id}: unknown key '{choice}' (use ENTER/c/q)")

        if mode == "with_prev":
            return

        if mode == "after_prev":
            delay_s = float(start.get("delay_s", 0))
            if delay_s > 0:
                self._log(f"[{index + 1}/{total}] {step_id}: waiting {delay_s:.2f}s before execution")
                self.sleep_func(delay_s)
            return

        raise RuntimeError(f"unsupported start mode: {mode}")

    def _execute_step_action(self, step: Dict[str, Any]) -> Dict[str, Any]:
        action = step.get("action") or {}
        action_type = str(action.get("type") or "").strip().lower()
        timeout_s = float(step.get("request_timeout_s", self.defaults.get("request_timeout_s", 12)))

        if action_type == "pause":
            seconds = float(action.get("seconds", 0))
            self._log(f"Pause: sleeping {seconds:.2f}s")
            if seconds > 0:
                self.sleep_func(seconds)
            return {"ok": True, "status": "accepted", "action": "pause"}

        if action_type == "ppt":
            if not self.ppt_enabled:
                raise RuntimeError("ppt action requires top-level ppt.enabled=true")

            controller = self._get_ppt_controller()
            ppt_mode = str(action.get("mode") or "").strip().lower()
            try:
                if ppt_mode == "next_build":
                    controller.next_build()
                elif ppt_mode == "prev_build":
                    controller.prev_build()
                elif ppt_mode == "goto":
                    slide = int(action.get("slide"))
                    build = action.get("build")
                    controller.goto(slide, int(build) if build is not None else None)
                else:
                    raise RuntimeError(f"unsupported ppt mode: {ppt_mode}")
            except PPTControllerError as exc:
                raise RuntimeError(str(exc)) from exc

            pos = self._get_ppt_position()
            self._last_script_ppt_position = dict(pos)
            self._log(f"[PPT] {self._format_position(pos)}")
            return {
                "ok": True,
                "status": "accepted",
                "action": "ppt",
                "mode": ppt_mode,
                "position": dict(pos),
            }

        robot_id = str(step.get("robot_id") or "").strip()
        if robot_id not in self.clients:
            raise RuntimeError(f"unknown robot_id: {robot_id}")
        client = self.clients[robot_id]

        if action_type == "say":
            text = str(action.get("text") or "").strip()
            return client.script_say(text=text, timeout_s=timeout_s)

        if action_type != "do":
            raise RuntimeError(f"unsupported action type: {action_type}")

        do_mode = str(action.get("mode") or "").strip().lower()
        payload: Dict[str, Any] = {"mode": do_mode}
        if do_mode == "command":
            payload["label"] = action.get("label")
            if isinstance(action.get("resolved"), dict):
                payload["resolved"] = action.get("resolved")
        elif do_mode == "dance":
            payload["dance_key"] = action.get("dance_key")
        elif do_mode in {"behavior_start", "behavior_stop"}:
            payload["behavior"] = action.get("behavior")
        else:
            raise RuntimeError(f"unsupported do.mode: {do_mode}")
        return client.script_do(payload=payload, timeout_s=timeout_s)

    def _ask_error_action(self) -> str:
        while True:
            try:
                choice = str(self.input_func("Step failed. Choose: [r]etry / [n]ext / [a]bort: ") or "").strip().lower()
            except EOFError as exc:
                raise RuntimeError("Cannot prompt for retry/next/abort without interactive stdin") from exc
            if choice in {"r", "retry"}:
                return "retry"
            if choice in {"n", "next"}:
                return "next"
            if choice in {"a", "abort"}:
                return "abort"

    def _on_error_action(self, step: Dict[str, Any]) -> str:
        policy = str(step.get("on_error") or self.defaults.get("on_error", "prompt")).strip().lower()
        if policy == "abort":
            return "abort"
        if policy == "continue":
            return "next"
        return self._ask_error_action()

    @staticmethod
    def _step_start_mode(step: Dict[str, Any]) -> str:
        start = step.get("start") or {}
        return str(start.get("mode") or "").strip().lower()

    def _execute_step_with_policy(self, step: Dict[str, Any], index: int, total: int) -> str:
        step_id = step.get("id", f"step_{index+1}")
        while True:
            try:
                self._pause_while_capture_off()
                self._check_capture_sync()
                self._log(f"[{index + 1}/{total}] {step_id}: executing")
                response = self._execute_step_action(step)
                self._log(f"[{index + 1}/{total}] {step_id}: OK -> {response}")
                return "completed"
            except _RunAbortRequested:
                raise
            except Exception as exc:
                self._log(f"[{index + 1}/{total}] {step_id}: ERROR -> {exc}")
                action = self._on_error_action(step)
                if action == "retry":
                    self._log(f"[{index + 1}/{total}] {step_id}: retry")
                    continue
                if action == "next":
                    self._log(f"[{index + 1}/{total}] {step_id}: skip to next")
                    return "skipped"
                self._log(f"[{index + 1}/{total}] {step_id}: abort requested")
                raise _RunAbortRequested(f"[{index + 1}/{total}] {step_id}: abort requested")

    def _execute_parallel_group_once(
        self,
        group: list[tuple[int, Dict[str, Any]]],
        total: int,
    ) -> tuple[int, list[tuple[int, Dict[str, Any], Exception]]]:
        completed = 0
        failures: list[tuple[int, Dict[str, Any], Exception]] = []
        max_workers = max(1, len(group))
        future_map: Dict[Any, tuple[int, Dict[str, Any], str]] = {}

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for step_index, step in group:
                step_id = step.get("id", f"step_{step_index+1}")
                self._log(f"[{step_index + 1}/{total}] {step_id}: executing (with_prev)")
                future = executor.submit(self._execute_step_action, step)
                future_map[future] = (step_index, step, step_id)

            for future in as_completed(future_map):
                step_index, step, step_id = future_map[future]
                try:
                    response = future.result()
                except Exception as exc:
                    failures.append((step_index, step, exc))
                    continue
                self._log(f"[{step_index + 1}/{total}] {step_id}: OK -> {response}")
                completed += 1

        failures.sort(key=lambda item: item[0])
        return completed, failures

    def _resolve_failed_step(
        self,
        step: Dict[str, Any],
        index: int,
        total: int,
        exc: Exception,
    ) -> str:
        step_id = step.get("id", f"step_{index+1}")
        self._log(f"[{index + 1}/{total}] {step_id}: ERROR -> {exc}")
        while True:
            action = self._on_error_action(step)
            if action == "retry":
                self._log(f"[{index + 1}/{total}] {step_id}: retry")
                return self._execute_step_with_policy(step, index, total)
            if action == "next":
                self._log(f"[{index + 1}/{total}] {step_id}: skip to next")
                return "skipped"
            self._log(f"[{index + 1}/{total}] {step_id}: abort requested")
            raise _RunAbortRequested(f"[{index + 1}/{total}] {step_id}: abort requested")

    def run(self) -> RunResult:
        steps = list(self.script.get("steps") or [])
        completed = 0
        total = len(steps)
        aborted = False
        try:
            self._prepare_ppt_if_needed()
            index = 0
            while index < total:
                step = steps[index]
                self._pause_while_capture_off()
                self._check_capture_sync()
                self._wait_for_step_start(step, index, total)
                self._pause_while_capture_off()
                self._check_capture_sync()

                group: list[tuple[int, Dict[str, Any]]] = [(index, step)]
                next_index = index + 1
                while next_index < total and self._step_start_mode(steps[next_index]) == "with_prev":
                    group.append((next_index, steps[next_index]))
                    next_index += 1

                if len(group) == 1:
                    status = self._execute_step_with_policy(step, index, total)
                    if status == "completed":
                        completed += 1
                    index = next_index
                    continue

                group_completed, failures = self._execute_parallel_group_once(group, total)
                completed += group_completed
                for failed_index, failed_step, failed_exc in failures:
                    status = self._resolve_failed_step(
                        failed_step,
                        failed_index,
                        total,
                        failed_exc,
                    )
                    if status == "completed":
                        completed += 1

                index = next_index
        except _RunAbortRequested as exc:
            aborted = True
            self._log(str(exc))
        finally:
            self._log_handle.close()

        return RunResult(
            completed_steps=completed,
            total_steps=total,
            aborted=aborted,
            log_path=self.log_path,
        )
