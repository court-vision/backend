"""Shim over cv_core.scoring_vocab — the shared implementation lives in cv-core.

Kept so the repo's own import paths stay stable while consumers migrate;
data-platform carries the same shim, and the pair is watched by its
mirror-drift guard. Do not add code here — change cv-core and bump the pin.
"""

from cv_core.scoring_vocab import (  # noqa: F401
    COUNTING_KEYS,
    DEFAULT_CATEGORIES,
    DEFAULT_POINT_WEIGHTS,
    ESPN_ID_TO_KEY,
    RATE_KEYS,
    STATS,
    StatDef,
    YAHOO_COMPOSITE_IDS,
    YAHOO_ID_TO_KEY,
    label_for,
)
