from datetime import datetime
from .espn_helpers import remove_diacritics

def calculate_fantasy_points(stats):
    """Formula for the fantasy points of a player"""
    points_score = stats['pts']
    rebounds_score = stats['reb']
    assists_score = stats['ast'] * 2
    stocks_score = (stats['stl'] + stats['blk']) * 4
    turnovers_score = stats['tov'] * -2
    three_pointers_score = stats['fg3m']
    fg_eff_score = (stats['fgm'] * 2) - stats['fga']
    ft_eff_score = stats['ftm'] - stats['fta']
    return points_score + rebounds_score + assists_score + stocks_score + turnovers_score + three_pointers_score + fg_eff_score + ft_eff_score

def create_daily_entry(old, new):
    """Creates the formatted entry for insertion into daily_stats"""
    return (old['id'],
             old['name'],
             old['team'],
             new['date'],
             new['fpts'] - old['fpts'],
             new['pts'] - old['pts'],
             new['reb'] - old['reb'],
             new['ast'] - old['ast'],
             new['stl'] - old['stl'],
             new['blk'] - old['blk'],
             new['tov'] - old['tov'],
             new['fgm'] - old['fgm'],
             new['fga'] - old['fga'],
             new['fg3m'] - old['fg3m'],
             new['fg3a'] - old['fg3a'],
             new['ftm'] - old['ftm'],
             new['fta'] - old['fta'],
             new['min'] - old['min'],
             new['rost_pct']
    )

def create_single_daily_entry(new):
    """Creates the formatted entry for insertion into daily_stats for a player who played but is not in the database"""
    return (new['id'],
                 new['name'],
                 new['team'],
                 new['date'],
                new['fpts'],
                new['pts'],
                new['reb'],
                new['ast'],
                new['stl'],
                new['blk'],
                new['tov'],
                new['fgm'],
                new['fga'],
                new['fg3m'],
                new['fg3a'],
                new['ftm'],
                new['fta'],
                new['min'],
                new['rost_pct']
    )

def create_daily_entries(had_game: list, old_dict: dict, date: datetime) -> list:
    """Creates the formatted entries for insertion into daily_stats"""
    entries = []

    for d in had_game:
        d['fpts'] = calculate_fantasy_points(d)
        d['date'] = date
        if d['id'] in old_dict:
            entries.append(create_daily_entry(old_dict[d['id']], d))
        else:
            entries.append(create_single_daily_entry(d))
        
    return entries

def create_total_entries(updated_dict: dict, old_dict: dict, id_map: set, today: datetime) -> list:
    """Creates the formatted entries for insertion into total_stats"""
    return [
        (
            id,
            d['name'],
            d['team'],
            today if id in id_map else old_dict[id]['date'],
            calculate_fantasy_points(d),
            d['pts'], d['reb'], d['ast'], d['stl'], d['blk'],
            d['tov'], d['fgm'], d['fga'], d['fg3m'], d['fg3a'],
            d['ftm'], d['fta'], d['min'], d['gp'], d['rost_pct']
        )
        for id, d in updated_dict.items()
    ]

def restructure_data(data: list) -> dict:
    """Takes in the raw data from the database and returns a restructured dict that looks the same as the NBA API data"""
    old_dict = {}
    for player in data:
        old_dict[player[0]] = {
            'id': player[0],
            'name': player[1],
            'team': player[2],
            'date': player[3],
            'fpts': player[4],
            'pts': player[5],
            'reb': player[6],
            'ast': player[7],
            'stl': player[8],
            'blk': player[9],
            'tov': player[10],
            'fgm': player[11],
            'fga': player[12],
            'fg3m': player[13],
            'fg3a': player[14],
            'ftm': player[15],
            'fta': player[16],
            'min': player[17],
            'gp': player[18],
            'c_rank': player[19],
            'p_rank': player[20]
        }

    return old_dict

def get_players_to_update(api_data: dict, db_data: dict) -> tuple:
    """Compare the data from the NBA API and the database to find the players who played"""
    had_game = []
    id_map = set()
    for id, d in api_data.items():
        # If the player is not in the db data, we know they played
        if id not in db_data:
            had_game.append(d)
            id_map.add(id)
            continue
        # If the player is in the old data, but 'gp' is different, we know they played
        if d['gp'] != db_data[id]['gp']:
            had_game.append(d)
            id_map.add(id)
    
    return had_game, id_map

def serialize_fpts_data(data: list) -> list:
    """Serialize FPTS data for response"""
    from app.schemas.etl import FPTSPlayer
    
    return [FPTSPlayer(
        rank=player[0],
        player_id=player[1],
        player_name=player[2],
        total_fpts=player[3],
        avg_fpts=player[4],
        rank_change=player[5]
    ) for player in data]
