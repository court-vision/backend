"""Shim over cv_core.season — the shared implementation lives in cv-core.

Kept so the repo's own import paths stay stable while consumers migrate;
data-platform carries the same shim, and the pair is watched by its
mirror-drift guard. Do not add code here — change cv-core and bump the pin.
"""

from cv_core.season import (  # noqa: F401
    espn_year_for,
    next_season,
    previous_season,
    season_for_date,
    season_from_year,
    season_key,
    season_label,
    short_key,
    start_year,
    validate_season,
)
