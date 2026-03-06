from __future__ import annotations

import pytest

from dialog import pipeline_builder


def test_runtime_context_enabled_defaults_to_true_when_missing() -> None:
    cfg = {"llm": {"type": "echo"}}
    assert pipeline_builder._extract_runtime_context_enabled(cfg) is True


def test_runtime_context_enabled_can_be_disabled_explicitly() -> None:
    cfg = {
        "llm": {
            "params": {
                "context": {
                    "inject_runtime_context": False,
                }
            }
        }
    }
    assert pipeline_builder._extract_runtime_context_enabled(cfg) is False


def test_runtime_context_enabled_respects_explicit_true() -> None:
    cfg = {
        "llm": {
            "params": {
                "context": {
                    "inject_runtime_context": True,
                }
            }
        }
    }
    assert pipeline_builder._extract_runtime_context_enabled(cfg) is True


def test_runtime_context_enabled_raises_for_invalid_context_type() -> None:
    cfg = {"llm": {"params": {"context": "invalid"}}}
    with pytest.raises(ValueError):
        pipeline_builder._extract_runtime_context_enabled(cfg)

