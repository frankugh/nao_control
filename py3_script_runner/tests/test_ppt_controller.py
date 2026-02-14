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
    def __init__(self, window: _FakeWindow) -> None:
        self.ShowType = None
        self._window = window

    def Run(self):
        return self._window


class _FakePresentation:
    def __init__(self, settings: _FakeSettings) -> None:
        self.SlideShowSettings = settings


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
