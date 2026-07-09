"""Fix strategies: serializable anchored patch planners (plan §4.3).

Importing this package registers the three MVP strategies.
"""

from . import await_state, bump_timeout, testid_selector  # noqa: F401 (registration)
from .base import (  # noqa: F401
    REGISTRY,
    PatchOp,
    Strategy,
    StrategyError,
    apply_ops,
    candidate_strategies,
    compute_changes,
    get_strategy,
    unified_diff,
)
