from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import subprocess
import sys
import threading
import traceback
import webbrowser
from collections import deque
from dataclasses import dataclass
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlsplit

if __package__ in (None, ""):
    _REPO_ROOT = Path(__file__).resolve().parent.parent
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    from py3_script_runner.runner import ScriptRunner
    from py3_script_runner.schema import ScriptSchemaError, validate_script
else:
    from .runner import ScriptRunner
    from .schema import ScriptSchemaError, validate_script


MODULE_DIR = Path(__file__).resolve().parent
REPO_ROOT = MODULE_DIR.parent
WEB_ROOT = MODULE_DIR / "script_builder_web"
SCRIPTS_DIR = MODULE_DIR / "scripts"
DM_DIR = REPO_ROOT / "py3_dialog_manager"
DM_PYTHON = DM_DIR / "venv" / "Scripts" / "python.exe"
DM_WEBAPP = DM_DIR / "webapp_server.py"
CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
EXAMPLE_FILES = {
    "example_workshop.json",
    "example_workshop_ppt.json",
    "example_workshop_summary.json",
}
ACTIVE_RUN_STATUSES = {"preflight", "running", "waiting"}


@dataclass(frozen=True)
class DmLaunchSpec:
    robot_id: str
    dm_url: str
    bind_host: str
    port: int
    instance_id: str


def _local_dm_bind_host(raw_host: str) -> Optional[str]:
    host = str(raw_host or "").strip()
    if not host:
        return None
    if host.casefold() == "localhost":
        return "127.0.0.1"
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        return None
    if parsed.is_loopback or parsed.is_unspecified:
        return host
    return None


def _dm_launcher_prereq_error() -> str:
    if not DM_DIR.exists():
        return f"DM map niet gevonden: {DM_DIR}"
    if not DM_PYTHON.exists():
        return f"DM venv python.exe niet gevonden: {DM_PYTHON}"
    if not DM_WEBAPP.exists():
        return f"DM webapp_server.py niet gevonden: {DM_WEBAPP}"
    return ""


def _dm_launch_result(
    *,
    robot_id: str,
    dm_url: str,
    instance_id: str,
    started: bool,
    message: str,
    port: Optional[int] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "robot_id": str(robot_id or ""),
        "dm_url": str(dm_url or ""),
        "instance_id": str(instance_id or ""),
        "started": bool(started),
        "message": str(message or ""),
    }
    if port is not None:
        result["port"] = int(port)
    if not started:
        result["error"] = str(message or "")
    return result


def _parse_dm_launch_spec(robot_id: str, robot_cfg: Dict[str, Any]) -> tuple[Optional[DmLaunchSpec], Optional[str]]:
    dm_url = str(robot_cfg.get("dm_url") or "").strip()
    instance_id = str(robot_cfg.get("instance_id") or "").strip()
    try:
        parsed = urlsplit(dm_url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        return None, f"robots.{robot_id}.dm_url is ongeldig: {exc}"
    if parsed.scheme not in {"http", "https"} or not host:
        return None, f"robots.{robot_id}.dm_url moet een volledige http(s) URL zijn."
    if port is None:
        return None, f"robots.{robot_id}.dm_url moet een expliciete poort bevatten."
    bind_host = _local_dm_bind_host(host)
    if bind_host is None:
        return None, f"robots.{robot_id}.dm_url moet naar een lokale host wijzen om een DM te starten."
    return DmLaunchSpec(
        robot_id=str(robot_id or ""),
        dm_url=dm_url,
        bind_host=bind_host,
        port=int(port),
        instance_id=instance_id,
    ), None


def _build_dm_launch_command(spec: DmLaunchSpec) -> List[str]:
    python_cmd = [str(DM_PYTHON), str(DM_WEBAPP), "--host", spec.bind_host, "--port", str(spec.port)]
    if spec.instance_id:
        python_cmd.extend(["--instance-id", spec.instance_id])
    return ["cmd.exe", "/k", subprocess.list2cmdline(python_cmd)]


def _local_target_is_occupied(spec: DmLaunchSpec) -> bool:
    probe_host = spec.bind_host
    if probe_host == "0.0.0.0":
        probe_host = "127.0.0.1"
    elif probe_host == "::":
        probe_host = "::1"
    try:
        with socket.create_connection((probe_host, spec.port), timeout=0.25):
            return True
    except OSError:
        return False


def _start_dm_process(spec: DmLaunchSpec) -> Dict[str, Any]:
    cmd = _build_dm_launch_command(spec)
    try:
        subprocess.Popen(
            cmd,
            cwd=str(DM_DIR),
            creationflags=CREATE_NEW_CONSOLE,
        )
    except OSError as exc:
        return _dm_launch_result(
            robot_id=spec.robot_id,
            dm_url=spec.dm_url,
            instance_id=spec.instance_id,
            started=False,
            message=f"DM starten mislukt: {exc}",
            port=spec.port,
        )
    return _dm_launch_result(
        robot_id=spec.robot_id,
        dm_url=spec.dm_url,
        instance_id=spec.instance_id,
        started=True,
        message="DM gestart in een nieuw cmd-venster.",
        port=spec.port,
    )


def start_dialog_managers(payload: Dict[str, Any]) -> tuple[int, Dict[str, Any]]:
    script_raw = payload.get("script") if isinstance(payload, dict) else None
    if not isinstance(script_raw, dict):
        return HTTPStatus.BAD_REQUEST, RunSessionManager._error_payload("Expected JSON body with object field 'script'.")
    try:
        script = validate_script(script_raw)
    except ScriptSchemaError as exc:
        return HTTPStatus.BAD_REQUEST, RunSessionManager._error_payload(f"Schema error: {exc}")

    prereq_error = _dm_launcher_prereq_error()
    if prereq_error:
        return HTTPStatus.INTERNAL_SERVER_ERROR, RunSessionManager._error_payload(prereq_error)

    robot_items = list((script.get("robots") or {}).items())
    results: List[Optional[Dict[str, Any]]] = [None] * len(robot_items)
    specs_by_index: Dict[int, DmLaunchSpec] = {}
    target_map: Dict[int, List[int]] = {}

    for index, (robot_id, robot_cfg_raw) in enumerate(robot_items):
        robot_cfg = robot_cfg_raw if isinstance(robot_cfg_raw, dict) else {}
        spec, error = _parse_dm_launch_spec(str(robot_id or ""), robot_cfg)
        if error:
            results[index] = _dm_launch_result(
                robot_id=str(robot_id or ""),
                dm_url=str(robot_cfg.get("dm_url") or ""),
                instance_id=str(robot_cfg.get("instance_id") or "").strip(),
                started=False,
                message=error,
            )
            continue
        specs_by_index[index] = spec
        target_map.setdefault(spec.port, []).append(index)

    for positions in target_map.values():
        if len(positions) < 2:
            continue
        first = specs_by_index[positions[0]]
        target_label = f"{first.bind_host}:{first.port}"
        for pos in positions:
            spec = specs_by_index[pos]
            results[pos] = _dm_launch_result(
                robot_id=spec.robot_id,
                dm_url=spec.dm_url,
                instance_id=spec.instance_id,
                started=False,
                message=f"Dubbele lokale DM target in script: {target_label}.",
                port=spec.port,
            )

    for index, spec in specs_by_index.items():
        if results[index] is not None:
            continue
        if _local_target_is_occupied(spec):
            results[index] = _dm_launch_result(
                robot_id=spec.robot_id,
                dm_url=spec.dm_url,
                instance_id=spec.instance_id,
                started=False,
                message=f"Er draait al iets op {spec.dm_url}.",
                port=spec.port,
            )
            continue
        results[index] = _start_dm_process(spec)

    final_results = [item for item in results if item is not None]
    started_count = sum(1 for item in final_results if item.get("started"))
    error_count = len(final_results) - started_count
    if started_count and not error_count:
        message = f"{started_count} DM{'s' if started_count != 1 else ''} gestart."
    elif started_count and error_count:
        message = f"{started_count} DM gestart, {error_count} fout."
    else:
        message = "Geen DM's gestart."
    return HTTPStatus.OK, {
        "ok": True,
        "results": final_results,
        "started_count": started_count,
        "error_count": error_count,
        "message": message,
    }


class RunSessionManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._run_thread: Optional[threading.Thread] = None
        self._abort_event: Optional[threading.Event] = None
        self._continue_event: Optional[threading.Event] = None
        self._run_counter = 0
        self._log_tail: deque[str] = deque(maxlen=300)
        self._state: Dict[str, Any] = {
            "status": "idle",
            "run_id": "",
            "waiting_for_next": False,
            "waiting_reason": "none",
            "current_step_index": None,
            "current_step_id": "",
            "completed_steps": 0,
            "total_steps": 0,
            "last_error": None,
            "log_path": "",
        }

    @staticmethod
    def _error_payload(message: str) -> Dict[str, Any]:
        return {"ok": False, "error": str(message)}

    def _snapshot_locked(self) -> Dict[str, Any]:
        snapshot = dict(self._state)
        snapshot["log_tail"] = list(self._log_tail)
        snapshot["ok"] = True
        return snapshot

    def state(self) -> Dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    def _start_new_run_locked(self, script: Dict[str, Any]) -> Dict[str, Any]:
        self._run_counter += 1
        run_id = f"run_{self._run_counter}"
        self._abort_event = threading.Event()
        self._continue_event = threading.Event()
        self._log_tail.clear()
        self._state = {
            "status": "preflight",
            "run_id": run_id,
            "waiting_for_next": False,
            "waiting_reason": "none",
            "current_step_index": None,
            "current_step_id": "",
            "completed_steps": 0,
            "total_steps": len(list(script.get("steps") or [])),
            "last_error": None,
            "log_path": "",
        }
        thread = threading.Thread(target=self._run_worker, args=(run_id, script), daemon=True)
        self._run_thread = thread
        thread.start()
        return self._snapshot_locked()

    def start(self, payload: Dict[str, Any]) -> tuple[int, Dict[str, Any]]:
        script_raw = payload.get("script") if isinstance(payload, dict) else None
        if not isinstance(script_raw, dict):
            return HTTPStatus.BAD_REQUEST, self._error_payload("Expected JSON body with object field 'script'.")
        try:
            script = validate_script(script_raw)
        except ScriptSchemaError as exc:
            return HTTPStatus.BAD_REQUEST, self._error_payload(f"Schema error: {exc}")

        with self._lock:
            status = str(self._state.get("status") or "")
            if status in ACTIVE_RUN_STATUSES:
                return HTTPStatus.CONFLICT, self._error_payload("A run is already active.")
            snapshot = self._start_new_run_locked(script)
            return HTTPStatus.ACCEPTED, snapshot

    def request_next(self) -> tuple[int, Dict[str, Any]]:
        with self._lock:
            status = str(self._state.get("status") or "")
            waiting_for_next = bool(self._state.get("waiting_for_next"))
            if status not in ACTIVE_RUN_STATUSES or not waiting_for_next or self._continue_event is None:
                return HTTPStatus.CONFLICT, self._error_payload("Run is not waiting for next.")
            self._continue_event.set()
            return HTTPStatus.ACCEPTED, self._snapshot_locked()

    def request_abort(self) -> tuple[int, Dict[str, Any]]:
        with self._lock:
            status = str(self._state.get("status") or "")
            if status not in ACTIVE_RUN_STATUSES:
                return HTTPStatus.CONFLICT, self._error_payload("No active run.")
            if self._abort_event is not None:
                self._abort_event.set()
            self._state["last_error"] = "Abort requested from web UI."
            return HTTPStatus.ACCEPTED, self._snapshot_locked()

    def _handle_runner_event(self, run_id: str, event: Dict[str, Any]) -> None:
        event_type = str(event.get("type") or "").strip().lower()
        with self._lock:
            if run_id != str(self._state.get("run_id") or ""):
                return
            if event_type == "log":
                msg = str(event.get("message") or "").strip()
                if msg:
                    self._log_tail.append(msg)
                return
            if event_type == "status":
                status = str(event.get("status") or "").strip().lower()
                if status in {"preflight", "running", "waiting", "completed", "aborted", "failed"}:
                    self._state["status"] = status
                if "completed_steps" in event:
                    self._state["completed_steps"] = int(event.get("completed_steps") or 0)
                if "total_steps" in event:
                    self._state["total_steps"] = int(event.get("total_steps") or 0)
                err = str(event.get("error") or "").strip()
                if err:
                    self._state["last_error"] = err
                if status in {"completed", "aborted", "failed"}:
                    self._state["waiting_for_next"] = False
                    self._state["waiting_reason"] = "none"
                return
            if event_type == "waiting":
                self._state["status"] = "waiting"
                self._state["waiting_for_next"] = True
                self._state["waiting_reason"] = str(event.get("waiting_reason") or "none")
                self._state["current_step_index"] = int(event.get("index")) if event.get("index") is not None else None
                self._state["current_step_id"] = str(event.get("step_id") or "")
                return
            if event_type == "waiting_cleared":
                self._state["waiting_for_next"] = False
                self._state["waiting_reason"] = "none"
                if str(self._state.get("status") or "") == "waiting":
                    self._state["status"] = "running"
                return
            if event_type == "step_start":
                self._state["status"] = "running"
                self._state["current_step_index"] = int(event.get("index")) if event.get("index") is not None else None
                self._state["current_step_id"] = str(event.get("step_id") or "")
                if event.get("total") is not None:
                    self._state["total_steps"] = int(event.get("total") or 0)
                return
            if event_type == "step_done":
                self._state["current_step_index"] = int(event.get("index")) if event.get("index") is not None else None
                self._state["current_step_id"] = str(event.get("step_id") or "")
                if "completed_steps" in event:
                    self._state["completed_steps"] = int(event.get("completed_steps") or 0)
                elif str(event.get("status") or "") == "completed":
                    self._state["completed_steps"] = int(self._state.get("completed_steps") or 0) + 1
                if event.get("total") is not None:
                    self._state["total_steps"] = int(event.get("total") or 0)
                return
            if event_type == "step_error":
                err = str(event.get("error") or "").strip()
                if err:
                    self._state["last_error"] = err

    def _set_terminal_failure(self, run_id: str, error_message: str) -> None:
        with self._lock:
            if run_id != str(self._state.get("run_id") or ""):
                return
            self._state["status"] = "failed"
            self._state["waiting_for_next"] = False
            self._state["waiting_reason"] = "none"
            self._state["last_error"] = str(error_message)
            self._log_tail.append(f"[RUN] FAILED: {error_message}")

    def _run_worker(self, run_id: str, script: Dict[str, Any]) -> None:
        abort_event: Optional[threading.Event]
        continue_event: Optional[threading.Event]
        with self._lock:
            abort_event = self._abort_event
            continue_event = self._continue_event

        runner = ScriptRunner(
            script,
            continue_event=continue_event,
            abort_event=abort_event,
            on_error_prompt_policy="next",
            summary_publish_policy="publish",
            ppt_mismatch_policy="defer_snapback",
            event_sink=lambda event: self._handle_runner_event(run_id, event),
        )
        try:
            runner.preflight()
            result = runner.run()
        except Exception as exc:
            tb = traceback.format_exc(limit=1)
            tb_line = tb.strip().splitlines()[-1] if tb else ""
            message = str(exc or "Run failed")
            if tb_line and tb_line != message:
                message = f"{message} ({tb_line})"
            self._set_terminal_failure(run_id, message)
            return

        with self._lock:
            if run_id != str(self._state.get("run_id") or ""):
                return
            self._state["completed_steps"] = int(result.completed_steps)
            self._state["total_steps"] = int(result.total_steps)
            self._state["waiting_for_next"] = False
            self._state["waiting_reason"] = "none"
            self._state["log_path"] = str(result.log_path)
            self._state["status"] = "aborted" if result.aborted else "completed"


RUN_SESSIONS = RunSessionManager()


class ScriptBuilderHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> tuple[bool, Dict[str, Any], str]:
        raw_len = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_len)
        except Exception:
            return False, {}, "Invalid Content-Length header."
        if length <= 0:
            return False, {}, "Request body is required."
        try:
            data = self.rfile.read(length)
        except Exception as exc:
            return False, {}, f"Could not read request body: {exc}"
        try:
            parsed = json.loads(data.decode("utf-8"))
        except Exception as exc:
            return False, {}, f"Invalid JSON body: {exc}"
        if not isinstance(parsed, dict):
            return False, {}, "JSON root must be an object."
        return True, parsed, ""

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/examples/"):
            name = parsed.path[len("/examples/") :]
            self._serve_example_file(name)
            return
        if parsed.path == "/api/run/state":
            self._send_json(HTTPStatus.OK, RUN_SESSIONS.state())
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        if parsed.path == "/api/dm/start":
            ok, payload, error = self._read_json_body()
            if not ok:
                self._send_json(HTTPStatus.BAD_REQUEST, RunSessionManager._error_payload(error))
                return
            status, response = start_dialog_managers(payload)
            self._send_json(status, response)
            return
        if parsed.path == "/api/run/start":
            ok, payload, error = self._read_json_body()
            if not ok:
                self._send_json(HTTPStatus.BAD_REQUEST, RunSessionManager._error_payload(error))
                return
            status, response = RUN_SESSIONS.start(payload)
            self._send_json(status, response)
            return
        if parsed.path == "/api/run/next":
            status, response = RUN_SESSIONS.request_next()
            self._send_json(status, response)
            return
        if parsed.path == "/api/run/abort":
            status, response = RUN_SESSIONS.request_abort()
            self._send_json(status, response)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Unknown endpoint.")

    def _serve_example_file(self, raw_name: str) -> None:
        safe_name = Path(unquote(raw_name)).name
        if safe_name not in EXAMPLE_FILES:
            self.send_error(HTTPStatus.NOT_FOUND, "Example file not found.")
            return
        target = SCRIPTS_DIR / safe_name
        if not target.exists() or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Example file missing.")
            return

        try:
            payload = target.read_bytes()
        except OSError as exc:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the standalone Script Builder webapp.")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind (default: 127.0.0.1).")
    parser.add_argument("--port", default=8765, type=int, help="Port to bind (default: 8765).")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open a browser window automatically.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not WEB_ROOT.exists():
        print(f"Missing web root: {WEB_ROOT}", file=sys.stderr)
        return 2

    try:
        server = ThreadingHTTPServer((args.host, args.port), ScriptBuilderHandler)
    except OSError as exc:
        print(f"Failed to bind {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 3

    url = f"http://{args.host}:{args.port}/"
    print("Script Builder is running.")
    print(f"URL: {url}")
    print("Press Ctrl+C to stop.")

    if not args.no_browser:
        try:
            webbrowser.open(url, new=1)
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Script Builder...")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
