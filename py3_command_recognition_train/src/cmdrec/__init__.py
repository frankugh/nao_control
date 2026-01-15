"""Command recognition training package."""

from cmdrec.rules import STOP_RULES, stop_rules
from cmdrec.resolvers import resolve_dance, resolve_locomotion

__all__ = ["STOP_RULES", "stop_rules", "resolve_dance", "resolve_locomotion"]
