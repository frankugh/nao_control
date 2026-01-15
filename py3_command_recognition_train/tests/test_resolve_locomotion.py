from __future__ import annotations

from cmdrec.resolvers import resolve_locomotion


def test_resolve_locomotion() -> None:
    assert resolve_locomotion("draai naar links")["action"] == "turn_left"
    assert resolve_locomotion("loop naar rechts")["action"] == "right"
    assert resolve_locomotion("ga vooruit")["action"] == "forward"
    assert resolve_locomotion("ga stukje terug")["action"] == "back"
    assert resolve_locomotion("stoppen met lopen")["action"] == "exit"
