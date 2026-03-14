from __future__ import annotations

from pathlib import Path

import pytest

from py3_script_runner.ppt_controller import ComPptController, PPTControllerError


class _FakeView:
    def __init__(self) -> None:
        self._slide = 1
        self._build = 0

    def Next(self):
        self._build += 1

    def Previous(self):
        self._build = max(0, self._build - 1)

    def GotoSlide(self, slide: int):
        self._slide = int(slide)
        self._build = 0

    def GotoClick(self, build: int):
        self._build = int(build)

    @property
    def CurrentShowPosition(self):
        return self._slide

    def GetClickIndex(self):
        return self._build


class _FakeWindow:
    def __init__(self, is_fullscreen: bool, view: _FakeView) -> None:
        self.IsFullScreen = is_fullscreen
        self.View = view


class _FakeSettings:
    def __init__(self, window: _FakeWindow, on_run=None) -> None:
        self.ShowType = None
        self._window = window
        self._on_run = on_run
        self.run_calls = 0

    def Run(self):
        self.run_calls += 1
        if self._on_run is not None:
            self._on_run()
        return self._window


class _FakePresentation:
    def __init__(self, settings: _FakeSettings) -> None:
        self.SlideShowSettings = settings
        self.SlideShowWindow = settings._window


class _FakePresentations:
    def __init__(self, presentation: _FakePresentation) -> None:
        self._presentation = presentation

    def Open(self, _path: str, WithWindow=True):
        return self._presentation


class _FakeApp:
    def __init__(self, presentation: _FakePresentation, window: _FakeWindow) -> None:
        self.Visible = False
        self.Presentations = _FakePresentations(presentation)
        self._window = window

    def SlideShowWindows(self, _index: int):
        return self._window


class _FakeWin32:
    def __init__(self, app: _FakeApp) -> None:
        self._app = app

    def Dispatch(self, _name: str):
        return self._app


def _build_fake_com(is_fullscreen: bool):
    view = _FakeView()
    window = _FakeWindow(is_fullscreen=is_fullscreen, view=view)
    settings = _FakeSettings(window=window)
    presentation = _FakePresentation(settings=settings)
    app = _FakeApp(presentation=presentation, window=window)
    return settings, _FakeWin32(app)


def test_open_slideshow_uses_kiosk_mode_when_fullscreen_required(tmp_path, monkeypatch):
    ppt_file = tmp_path / "demo.pptx"
    ppt_file.write_text("x", encoding="utf-8")
    settings, fake_win32 = _build_fake_com(is_fullscreen=True)

    monkeypatch.setattr("py3_script_runner.ppt_controller.sys.platform", "win32")
    monkeypatch.setattr(ComPptController, "_win32_client", staticmethod(lambda: fake_win32))

    controller = ComPptController()
    controller.open_and_start_slideshow(str(ppt_file), fullscreen_required=True)
    assert settings.ShowType == 3


def test_open_slideshow_uses_window_mode_when_fullscreen_not_required(tmp_path, monkeypatch):
    ppt_file = tmp_path / "demo.pptx"
    ppt_file.write_text("x", encoding="utf-8")
    settings, fake_win32 = _build_fake_com(is_fullscreen=False)

    monkeypatch.setattr("py3_script_runner.ppt_controller.sys.platform", "win32")
    monkeypatch.setattr(ComPptController, "_win32_client", staticmethod(lambda: fake_win32))

    controller = ComPptController()
    controller.open_and_start_slideshow(str(ppt_file), fullscreen_required=False)
    assert settings.ShowType == 2


def test_open_slideshow_raises_when_fullscreen_required_but_not_fullscreen(tmp_path, monkeypatch):
    ppt_file = tmp_path / "demo.pptx"
    ppt_file.write_text("x", encoding="utf-8")
    _settings, fake_win32 = _build_fake_com(is_fullscreen=False)

    monkeypatch.setattr("py3_script_runner.ppt_controller.sys.platform", "win32")
    monkeypatch.setattr(ComPptController, "_win32_client", staticmethod(lambda: fake_win32))

    controller = ComPptController()
    with pytest.raises(PPTControllerError, match="not fullscreen"):
        controller.open_and_start_slideshow(str(ppt_file), fullscreen_required=True)


def test_open_slideshow_runs_when_new_presentation_has_no_slideshow_window_yet(tmp_path, monkeypatch):
    class _LazyPresentation:
        def __init__(self, window: _FakeWindow) -> None:
            self._started = False
            self.SlideShowSettings = _FakeSettings(window=window, on_run=self._mark_started)

        def _mark_started(self) -> None:
            self._started = True

        @property
        def SlideShowWindow(self):
            if not self._started:
                raise RuntimeError("no slideshow window yet")
            return self.SlideShowSettings._window

    ppt_file = tmp_path / "demo.pptx"
    ppt_file.write_text("x", encoding="utf-8")
    view = _FakeView()
    window = _FakeWindow(is_fullscreen=True, view=view)
    presentation = _LazyPresentation(window=window)
    app = _FakeApp(presentation=presentation, window=window)
    fake_win32 = _FakeWin32(app)

    monkeypatch.setattr("py3_script_runner.ppt_controller.sys.platform", "win32")
    monkeypatch.setattr(ComPptController, "_win32_client", staticmethod(lambda: fake_win32))

    controller = ComPptController()
    controller.open_and_start_slideshow(str(ppt_file), fullscreen_required=True)

    assert presentation.SlideShowSettings.run_calls == 1


def test_refresh_live_refs_prefers_presentation_window_before_global_window():
    class _FlakyView(_FakeView):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def Next(self):
            self.calls += 1
            raise RuntimeError("stale view")

    stale_view = _FlakyView()
    wrong_view = _FakeView()
    correct_view = _FakeView()
    wrong_window = _FakeWindow(is_fullscreen=True, view=wrong_view)
    correct_window = _FakeWindow(is_fullscreen=True, view=correct_view)
    settings = _FakeSettings(window=correct_window)
    presentation = _FakePresentation(settings=settings)
    app = _FakeApp(presentation=presentation, window=wrong_window)

    controller = ComPptController()
    controller._app = app
    controller._presentation = presentation
    controller._slide_show_window = wrong_window
    controller._view = stale_view

    controller.next_slide()

    assert wrong_view.GetClickIndex() == 0
    assert correct_view.GetClickIndex() == 1
