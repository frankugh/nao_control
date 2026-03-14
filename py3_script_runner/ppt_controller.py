from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Protocol
import sys


class PPTControllerError(RuntimeError):
    """Raised when the PowerPoint controller cannot perform an operation."""


class PptControllerProtocol(Protocol):
    def open_and_start_slideshow(self, file_path: str, fullscreen_required: bool = True) -> None:
        ...

    def next_slide(self) -> None:
        ...

    def previous_slide(self) -> None:
        ...

    def goto(self, slide: int, click: Optional[int] = None) -> None:
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

    @staticmethod
    def _co_initialize_current_thread() -> None:
        # COM must be initialized per thread (web runner uses a background thread).
        try:
            import pythoncom  # type: ignore[import-not-found]
        except Exception:
            return
        try:
            pythoncom.CoInitialize()
        except Exception:
            return

    def _require_view(self):
        if self._view is None:
            raise PPTControllerError("PowerPoint slideshow is not active.")
        return self._view

    def _require_window(self):
        if self._slide_show_window is None:
            raise PPTControllerError("PowerPoint slideshow window is not available.")
        return self._slide_show_window

    def _refresh_live_refs(self) -> None:
        app = self._app
        presentation = self._presentation
        candidates = []
        if presentation is not None:
            try:
                window = getattr(presentation, "SlideShowWindow", None)
            except Exception:
                window = None
            if window is not None:
                candidates.append(window)
        try:
            window = app.SlideShowWindows(1) if app is not None else None
        except Exception:
            window = None
        if window is not None:
            candidates.append(window)
        try:
            windows = getattr(app, "SlideShowWindows", None) if app is not None else None
            if windows is not None and hasattr(windows, "Item"):
                window = windows.Item(1)
            else:
                window = None
        except Exception:
            window = None
        if window is not None:
            candidates.append(window)
        for window in candidates:
            try:
                self._slide_show_window = window
                self._view = window.View
                return
            except Exception:
                continue

    @staticmethod
    def _find_open_presentation(app, ppt_path: Path):
        try:
            count = int(getattr(app.Presentations, "Count", 0))
        except Exception:
            return None
        target = ppt_path.as_posix().lower()
        for idx in range(count, 0, -1):
            try:
                existing = app.Presentations.Item(idx)
                full_name = str(getattr(existing, "FullName", "") or "").strip()
                if not full_name:
                    continue
                if Path(full_name).expanduser().resolve().as_posix().lower() == target:
                    return existing
            except Exception:
                continue
        return None

    def open_and_start_slideshow(self, file_path: str, fullscreen_required: bool = True) -> None:
        self._require_windows()
        self._co_initialize_current_thread()
        ppt_path = Path(file_path).expanduser().resolve()
        if not ppt_path.exists():
            raise PPTControllerError(f"PPT file not found: {ppt_path}")
        win32 = self._win32_client()
        try:
            dispatch = getattr(win32, "Dispatch", None)
            if dispatch is None:
                raise RuntimeError("win32com Dispatch unavailable")
            app = dispatch("PowerPoint.Application")
            app.Visible = True
            try:
                app.DisplayAlerts = 0
            except Exception:
                pass

            existing = self._find_open_presentation(app, ppt_path)
            if existing is not None:
                presentation = existing
            else:
                presentation = app.Presentations.Open(str(ppt_path), WithWindow=True)
            settings = presentation.SlideShowSettings
            # 3 = ppShowTypeKiosk (fullscreen), 2 = ppShowTypeWindow (windowed).
            settings.ShowType = 3 if fullscreen_required else 2
            window = None
            try:
                if existing is not None:
                    try:
                        window = getattr(presentation, "SlideShowWindow", None)
                        if window is not None:
                            _ = window.View
                    except Exception:
                        window = None
                if window is None:
                    window = settings.Run()
            except Exception:
                # Existing presentation can be in a stale state after repeated runs.
                # Try reopening once as fallback.
                if existing is None:
                    raise
                try:
                    presentation.Close()
                except Exception:
                    pass
                presentation = app.Presentations.Open(str(ppt_path), WithWindow=True)
                settings = presentation.SlideShowSettings
                settings.ShowType = 3 if fullscreen_required else 2
                window = settings.Run()
            if window is None:
                window = getattr(presentation, "SlideShowWindow", None)
            if window is None:
                window = app.SlideShowWindows(1)
            view = window.View
        except Exception as exc:
            raise PPTControllerError(f"Could not open PowerPoint slideshow: {exc}") from exc

        self._app = app
        self._presentation = presentation
        self._slide_show_window = window if window is not None else app.SlideShowWindows(1)
        self._view = view

        if fullscreen_required and not self.is_fullscreen_slideshow():
            raise PPTControllerError("PowerPoint slideshow is not fullscreen.")

    def next_slide(self) -> None:
        view = self._require_view()
        try:
            view.Next()
            return
        except Exception:
            self._refresh_live_refs()
        view = self._require_view()
        try:
            view.Next()
        except Exception as exc:
            raise PPTControllerError(f"PowerPoint next_slide failed: {exc}") from exc

    def previous_slide(self) -> None:
        view = self._require_view()
        try:
            view.Previous()
            return
        except Exception:
            self._refresh_live_refs()
        view = self._require_view()
        try:
            view.Previous()
        except Exception as exc:
            raise PPTControllerError(f"PowerPoint previous_slide failed: {exc}") from exc

    def goto(self, slide: int, click: Optional[int] = None) -> None:
        view = self._require_view()
        slide_idx = int(slide)
        if slide_idx <= 0:
            raise PPTControllerError("PowerPoint goto requires slide >= 1.")
        try:
            view.GotoSlide(slide_idx)
        except Exception:
            self._refresh_live_refs()
            view = self._require_view()
            try:
                view.GotoSlide(slide_idx)
            except Exception as exc:
                raise PPTControllerError(f"PowerPoint goto slide failed: {exc}") from exc
        if click is None:
            return
        click_idx = int(click)
        # Click index 0 means "slide default state"; no additional click navigation needed.
        if click_idx <= 0:
            return
        try:
            view.GotoClick(click_idx)
        except Exception:
            self._refresh_live_refs()
            view = self._require_view()
            try:
                view.GotoClick(click_idx)
            except Exception as exc:
                raise PPTControllerError(f"PowerPoint goto click failed: {exc}") from exc

    def get_position(self) -> Dict[str, int]:
        view = self._require_view()
        try:
            slide = int(view.CurrentShowPosition)
        except Exception:
            self._refresh_live_refs()
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
        try:
            value = getattr(window, "IsFullScreen", True)
        except Exception:
            self._refresh_live_refs()
            window = self._require_window()
            value = getattr(window, "IsFullScreen", True)
        try:
            return bool(value)
        except Exception:
            return True
