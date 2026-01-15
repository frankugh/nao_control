from __future__ import annotations

from cmdrec.rules import stop_rules


def test_stop_rules_match() -> None:
    assert stop_rules("stop ermee")
    assert stop_rules("halt alsjeblieft")
    assert stop_rules("ho nu")
    assert not stop_rules("ga verder")
