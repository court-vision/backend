"""
Fantasy Points Transformer

Calculates fantasy points using the league scoring formula.
"""

from typing import TypedDict, Union


class PlayerStats(TypedDict):
    """Type definition for player stats used in fantasy point calculation."""

    pts: int
    reb: int
    ast: int
    stl: int
    blk: int
    tov: int
    fgm: int
    fga: int
    fg3m: int
    ftm: int
    fta: int


def calculate_fantasy_points(stats: PlayerStats) -> int:
    """
    Calculate fantasy points using the league scoring formula.

    Scoring breakdown:
        - Points: 1 pt each
        - Rebounds: 1 pt each
        - Assists: 2 pts each
        - Steals: 4 pts each
        - Blocks: 4 pts each
        - Turnovers: -2 pts each
        - 3-pointers made: 1 pt each (bonus)
        - FG efficiency: (FGM * 2) - FGA
        - FT efficiency: FTM - FTA

    Args:
        stats: Dict with player stat values

    Returns:
        Total fantasy points as integer
    """
    points_score = stats["pts"]
    rebounds_score = stats["reb"]
    assists_score = stats["ast"] * 2
    stocks_score = (stats["stl"] + stats["blk"]) * 4
    turnovers_score = stats["tov"] * -2
    three_pointers_score = stats["fg3m"]
    fg_eff_score = (stats["fgm"] * 2) - stats["fga"]
    ft_eff_score = stats["ftm"] - stats["fta"]

    return int(
        points_score
        + rebounds_score
        + assists_score
        + stocks_score
        + turnovers_score
        + three_pointers_score
        + fg_eff_score
        + ft_eff_score
    )


def minutes_to_int(min_str: Union[str, int, float, None]) -> int:
    """
    Convert minutes to an integer from various formats.

    Handles:
    - ISO 8601 duration "PT34M56.00S" -> 34  (nba_api live BoxScore format)
    - String "34:56" -> 34                    (nba_api stats format)
    - Float 34.5 -> 34
    - Int 34 -> 34
    - None -> 0

    Args:
        min_str: Minutes value in various formats

    Returns:
        Integer minutes (truncated, not rounded)

    Examples:
        >>> minutes_to_int("PT34M56.00S")
        34
        >>> minutes_to_int("34:56")
        34
        >>> minutes_to_int(34.5)
        34
        >>> minutes_to_int(None)
        0
    """
    import re

    if min_str is None:
        return 0

    if isinstance(min_str, (int, float)):
        return int(min_str)

    s = str(min_str)

    # ISO 8601 duration format: "PT18M00.00S" (from nba_api live BoxScore)
    if s.startswith("PT"):
        match = re.match(r"PT(\d+)M", s)
        if match:
            return int(match.group(1))
        return 0

    # MM:SS format: "34:56" (from nba_api stats endpoints)
    if ":" in s:
        parts = s.split(":")
        return int(parts[0])

    try:
        return int(min_str)
    except (ValueError, TypeError):
        return 0
