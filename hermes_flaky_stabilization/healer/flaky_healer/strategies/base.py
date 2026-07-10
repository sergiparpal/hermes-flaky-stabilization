"""The Strategy protocol and the cause-matching base class.

The serializable patch type + apply/diff engine live in :mod:`patchops` and the
registry in :mod:`registry`; this module keeps only the strategy abstraction.
``PatchOp``/``StrategyError``/``register_strategy`` are re-exported here so the
concrete strategies (and their tests) can keep importing them ``from .base``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

# Re-exported for the concrete strategies + tests that import them ``from .base``.
from .patchops import PatchOp, StrategyError  # noqa: F401
from .registry import register_strategy  # noqa: F401


@runtime_checkable
class Strategy(Protocol):
    name: str

    def applies(self, diagnosis: dict, trace) -> bool: ...

    def plan(self, test_source: str, diagnosis: dict, trace, test_file: str) -> list[PatchOp]: ...


class CauseMatchStrategy:
    """Base for strategies gated on the diagnosed cause.

    A strategy applies when the diagnosis' ``cause`` equals the subclass'
    ``cause`` attribute OR its ``recommended_strategy`` names this strategy.
    The three MVP strategies had this predicate copy-pasted verbatim; sharing it
    here is what keeps them from drifting apart in how they read a diagnosis. A
    subclass that needs an EXTRA gate (e.g. proof of a data-testid on the target)
    overrides ``applies`` and ANDs its guard onto ``super().applies(...)``.

    Deliberately NOT declared as a subclass of the ``Strategy`` Protocol: this
    base has no ``plan``, so the concrete subclasses satisfy that protocol
    structurally by defining ``plan`` themselves (the registry/``get_strategy``
    keep working unchanged).
    """

    name: str
    cause: str

    def applies(self, diagnosis: dict, trace) -> bool:
        diagnosis = diagnosis or {}
        return (
            diagnosis.get("cause") == self.cause
            or diagnosis.get("recommended_strategy") == self.name
        )

    @staticmethod
    def _selector(trace, diagnosis: dict):
        """The failing selector: the trace's parsed selector, else the
        diagnosis' ``selector``.

        Folded here from two verbatim copies (await_state/bump_timeout) so both
        planners resolve the selector identically. Returns whatever the source
        holds (callers do their own ``isinstance(..., str)`` guarding, since a
        diagnosis selector can be an untrusted LLM value).
        """
        return (trace.selector if trace else None) or (diagnosis or {}).get("selector")
