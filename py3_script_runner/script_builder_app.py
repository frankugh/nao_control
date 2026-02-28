from __future__ import annotations

import argparse
import sys
import webbrowser
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


MODULE_DIR = Path(__file__).resolve().parent
WEB_ROOT = MODULE_DIR / "script_builder_web"
SCRIPTS_DIR = MODULE_DIR / "scripts"
EXAMPLE_FILES = {
    "example_workshop.json",
    "example_workshop_ppt.json",
    "example_workshop_summary.json",
}


class ScriptBuilderHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/examples/"):
            name = parsed.path[len("/examples/") :]
            self._serve_example_file(name)
            return
        super().do_GET()

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
