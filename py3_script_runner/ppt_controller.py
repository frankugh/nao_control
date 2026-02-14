from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Protocol
import sys


class PPTControllerError(RuntimeError):
    """Raised when the PowerPoint controller cannot perform an operation."""


class PptControllerProtocol(Protocol):
    def open_and_start_slideshow(self, file_path: str, fullscreen_required: bool = True) -> None:
        ...

    def next_build(self) -> None:
        ...

    def prev_build(self) -> None:
        ...

    def goto(self, slide: int, build: Optional[int] = None) -> None:
        ...

    def get_position(self) -> Dict[str, int]:
        ...

    def is_fullscreen_slideshow(self) -> bool:
        ...


class ComPptController:
    """PowerPoint controller using Windows COM automation."""

    def __init__(self) -> None:
        self._app = None
        self._presentation = None
        self._slide_show_window = None
        self._view = None

    @staticmethod
    def _require_windows() -> None:
        if sys.platform != "win32":
            raise PPTControllerError("PPT feature requires Windows + PowerPoint COM")

    @staticmethod
    def _win32_client():
        try:
            import win32com.client  # type: ignore[import-not-found]
        except Exception as exc:
            raise PPTControllerError(
                "PPT feature requires pywin32 (install dependency 'pywin32')."
            ) from exc
        return win32com.client

    def _require_view(self):
        if self._view is None:
            raise PPTControllerError("PowerPoint slideshow is not active.")
        return self._view

    def _require_window(self):
        if self._slide_show_window is None:
            raise PPTControllerError("PowerPoint slideshow window is not available.")
        return self._slide_show_window

    def open_and_start_slideshow(self, file_path: str, fullscreen_required: bool = True) -> None:
        self._require_windows()
        ppt_path = Path(file_path).expanduser().resolve()
        if not ppt_path.exists():
            raise PPTControllerError(f"PPT file not found: {ppt_path}")
        win32 = self._win32_client()
        try:
            app = win32.Dispatch("PowerPoint.Application")
            app.Visible = True
            presentation = app.Presentations.Open(str(ppt_path), WithWindow=True)
            settings = presentation.SlideShowSettings
            # 3 = ppShowTypeKiosk (fullscreen), 2 = ppShowTypeWindow (windowed).
            settings.ShowType = 3 if fullscreen_required else 2
            window = settings.Run()
            view = window.View if window is not None else app.SlideShowWindows(1).View
        except Exception as exc:
            raise PPTControllerError(f"Could not open PowerPoint slideshow: {exc}") from exc

        self._app = app
        self._presentation = presentation
        self._slide_show_window = window if window is not None else app.SlideShowWindows(1)
        self._view = view

        if fullscreen_required and not self.is_fullscreen_slideshow():
            raise PPTControllerError("PowerPoint slideshow is not fullscreen.")

    def next_build(self) -> None:
        view = self._require_view()
        try:
            view.Next()
        except Exception as exc:
            raise PPTControllerError(f"PowerPoint next_build failed: {exc}") from exc

    def prev_build(self) -> None:
        view = self._require_view()
        try:
            view.Previous()
        except Exception as exc:
            raise PPTControllerError(f"PowerPoint prev_build failed: {exc}") from exc

    def goto(self, slide: int, build: Optional[int] = None) -> None:
        view = self._require_view()
        slide_idx = int(slide)
        if slide_idx <= 0:
            raise PPTControllerError("PowerPoint goto requires slide >= 1.")
        try:
            view.GotoSlide(slide_idx)
        except Exception as exc:
            raise PPTControllerError(f"PowerPoint goto slide failed: {exc}") from exc
        if build is None:
            return
        build_idx = int(build)
        # Build index 0 means "slide default state"; no additional click navigation needed.
        if build_idx <= 0:
            return
        try:
            view.GotoClick(build_idx)
        except Exception as exc:
            raise PPTControllerError(f"PowerPoint goto build failed: {exc}") from exc

    def get_position(self) -> Dict[str, int]:
        view = self._require_view()
        try:
            slide = int(view.CurrentShowPosition)
        except Exception as exc:
            raise PPTControllerError(f"PowerPoint current slide unavailable: {exc}") from exc
        build = 0
        try:
            build = int(view.GetClickIndex())
        except Exception:
            build = 0
        return {"slide": slide, "build": max(0, build)}

    def is_fullscreen_slideshow(self) -> bool:
        window = self._require_window()
        value = getattr(window, "IsFullScreen", True)
        try:
            return bool(value)
        except Exception:
            return True
