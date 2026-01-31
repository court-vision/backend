"""
Yahoo Fantasy API helpers and data mappings.

Yahoo Fantasy API documentation:
https://developer.yahoo.com/fantasysports/guide/
"""

# ------------------------------------------ Yahoo Fantasy Data ------------------------------------------

# Yahoo position mappings
# Yahoo uses string position codes directly
YAHOO_POSITION_MAP = {
    "PG": "PG",
    "SG": "SG",
    "G": "G",
    "SF": "SF",
    "PF": "PF",
    "F": "F",
    "C": "C",
    "Util": "UT",
    "BN": "BE",
    "IL": "IR",
    "IL+": "IR",
}

# Yahoo stat category IDs to names
YAHOO_STAT_MAP = {
    "5": "FGA",
    "6": "FGM",
    "7": "FG%",
    "8": "FTA",
    "9": "FTM",
    "10": "FT%",
    "11": "3PTA",
    "12": "3PM",
    "13": "3PT%",
    "15": "PTS",
    "16": "OREB",
    "17": "DREB",
    "18": "REB",
    "19": "AST",
    "20": "ST",
    "21": "BLK",
    "22": "TO",
    "23": "A/TO",
    "24": "PF",
    "25": "DISQ",
    "26": "TECH",
    "27": "EJ",
    "28": "FF",
    "29": "MPG",
    "30": "DD",
    "31": "TD",
}

# Yahoo team abbreviations (they use standard NBA abbreviations)
YAHOO_TEAM_MAP = {
    "ATL": "ATL",
    "BOS": "BOS",
    "BKN": "BKN",
    "CHA": "CHA",
    "CHI": "CHI",
    "CLE": "CLE",
    "DAL": "DAL",
    "DEN": "DEN",
    "DET": "DET",
    "GS": "GSW",
    "GSW": "GSW",
    "HOU": "HOU",
    "IND": "IND",
    "LAC": "LAC",
    "LAL": "LAL",
    "MEM": "MEM",
    "MIA": "MIA",
    "MIL": "MIL",
    "MIN": "MIN",
    "NO": "NOP",
    "NOP": "NOP",
    "NY": "NYK",
    "NYK": "NYK",
    "OKC": "OKC",
    "ORL": "ORL",
    "PHI": "PHL",
    "PHL": "PHL",
    "PHO": "PHO",
    "PHX": "PHO",
    "POR": "POR",
    "SAC": "SAC",
    "SA": "SAS",
    "SAS": "SAS",
    "TOR": "TOR",
    "UTA": "UTA",
    "WAS": "WAS",
}

# Maps user-friendly window names to Yahoo stat types
YAHOO_AVG_WINDOW_MAP = {
    "season": "season",
    "last_7": "lastweek",
    "last_14": "lastmonth",
    "last_30": "lastmonth",
}


def normalize_team_abbr(yahoo_abbr: str) -> str:
    """Normalize Yahoo team abbreviation to standard format."""
    return YAHOO_TEAM_MAP.get(yahoo_abbr, yahoo_abbr)


def normalize_position(yahoo_position: str) -> str:
    """Normalize Yahoo position to standard format."""
    return YAHOO_POSITION_MAP.get(yahoo_position, yahoo_position)


def parse_yahoo_player_positions(eligible_positions: list[dict]) -> list[str]:
    """
    Parse Yahoo player eligible positions into standard position codes.

    Args:
        eligible_positions: List of position dicts from Yahoo API
            e.g., [{"position": "PG"}, {"position": "SG"}, {"position": "G"}]

    Returns:
        List of normalized position codes
    """
    positions = []
    for pos in eligible_positions:
        position = pos.get("position", "")
        normalized = normalize_position(position)
        if normalized and normalized not in positions:
            positions.append(normalized)
    return positions


def extract_yahoo_player_stats(player_stats: dict, stat_type: str = "season") -> dict:
    """
    Extract stats from Yahoo player stats response.

    Args:
        player_stats: Stats dict from Yahoo API
        stat_type: Type of stats to extract (season, lastweek, lastmonth)

    Returns:
        Dict of stat_name -> value
    """
    stats = {}
    if not player_stats:
        return stats

    for stat in player_stats.get("stats", []):
        stat_id = str(stat.get("stat_id", ""))
        value = stat.get("value", 0)
        stat_name = YAHOO_STAT_MAP.get(stat_id, f"stat_{stat_id}")
        try:
            stats[stat_name] = float(value) if value != "-" else 0.0
        except (ValueError, TypeError):
            stats[stat_name] = 0.0

    return stats


def parse_yahoo_team_key(team_key: str) -> dict:
    """
    Parse Yahoo team key into components.

    Yahoo team keys have format: {game_key}.l.{league_id}.t.{team_id}
    Example: "428.l.12345.t.3"

    Args:
        team_key: Yahoo team key string

    Returns:
        Dict with game_key, league_id, team_id
    """
    parts = team_key.split(".")
    if len(parts) >= 5:
        return {
            "game_key": parts[0],
            "league_id": parts[2],
            "team_id": parts[4],
        }
    return {"game_key": "", "league_id": "", "team_id": ""}


def build_yahoo_team_key(game_key: str, league_id: str, team_id: str) -> str:
    """
    Build Yahoo team key from components.

    Args:
        game_key: Yahoo game key (e.g., "428" for 2023-24 NBA)
        league_id: League ID
        team_id: Team ID within the league

    Returns:
        Formatted team key string
    """
    return f"{game_key}.l.{league_id}.t.{team_id}"
