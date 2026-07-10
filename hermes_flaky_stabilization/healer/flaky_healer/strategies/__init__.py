"""Fix strategies: serializable anchored patch planners (plan §4.3).

Importing this package registers the three MVP strategies.
"""

from . import await_state, bump_timeout, testid_selector  # noqa: F401 (registration)
from .base import CauseMatchStrategy, Strategy  # noqa: F401
from .patchops import (  # noqa: F401
    PatchOp,
    StrategyError,
    apply_ops,
    compute_changes,
    unified_diff,
)
from .registry import (  # noqa: F401
    REGISTRY,
    candidate_strategies,
    get_strategy,
    register_strategy,
)
