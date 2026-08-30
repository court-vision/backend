"""Shim over cv_core.nba_calendar — the shared implementation lives in cv-core.

Kept so the repo's own import paths stay stable while consumers migrate;
data-platform carries the same shim, and the pair is watched by its
mirror-drift guard. Do not add code here — change cv-core and bump the pin.
"""

from cv_core.nba_calendar import (  # noqa: F401
    DAY_ROLLOVER_HOUR_ET,
    EASTERN,
    nba_date_et,
)
