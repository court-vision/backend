"""Read the ingestion snapshots without league credentials or draft-room state."""

from datetime import date

from peewee import fn

from core.errors import NotFoundError
from core.settings import settings
from db.base import db_operation
from db.models.nba.draft_market import DraftMarket
from db.models.nba.player_projections import PlayerProjection
from db.models.nba.players import Player
from schemas.common import ApiStatus
from schemas.market import (
    ESPNMarketData, ESPNMarketMovementData, ESPNMarketMovementPlayer,
    ESPNMarketMovementResp, ESPNMarketPlayer, ESPNMarketResp, MarketChanges,
    MarketMeasurements, MarketMovementDirection, MarketSort, PlayerProjectionData,
    PlayerProjectionResp, PlayerProjectionsData, PlayerProjectionsResp, ProjectionStats,
)


def _snapshot_date(model, season: str, as_of: date | None = None):
    query = model.select(fn.MAX(model.as_of_date)).where(
        (model.season == season) & (model.source == "espn")
    )
    if as_of is not None:
        query = query.where(model.as_of_date <= as_of)
    return query.scalar()


def _projection_stats(row: PlayerProjection) -> ProjectionStats:
    """Serialize one projection row identically for bulk and player reads."""
    stats = {key: getattr(row, key) for key in PlayerProjection.STAT_KEYS}
    for rate, made, attempted in (
        ("fg_pct", "fgm", "fga"),
        ("fg3_pct", "fg3m", "fg3a"),
        ("ft_pct", "ftm", "fta"),
    ):
        stats[rate] = (
            round(float(stats[made] / stats[attempted]), 4)
            if stats[made] is not None
            and stats[attempted] is not None
            and stats[attempted] > 0
            else None
        )
    return ProjectionStats(**stats)


def _projection_data(
    row: PlayerProjection,
    player: Player,
    season: str,
    snapshot: date,
) -> PlayerProjectionData:
    return PlayerProjectionData(
        player_id=player.id,
        espn_id=player.espn_id,
        name=player.name,
        season=season,
        as_of_date=snapshot,
        projected_gp=row.projected_gp,
        stats=_projection_stats(row),
    )


def _market_measurements(row: dict, prefix: str) -> MarketMeasurements | None:
    if not row[f"{prefix}_present"]:
        return None
    return MarketMeasurements(**{
        field: row[f"{prefix}_{field}"]
        for field in ("overall_rank", "adp", "auction_value", "auction_value_avg")
    })


def _movement_value(row: dict, prefix: str, field: str):
    return row[f"{prefix}_{field}"] if row[f"{prefix}_present"] else None


def _change(before, after, *, lower_is_better: bool = False):
    if before is None or after is None:
        return None
    return before - after if lower_is_better else after - before


class PublicMarketService:
    @staticmethod
    @db_operation("market.espn")
    def get_market(
        season: str | None = None,
        as_of: date | None = None,
        name: str | None = None,
        sort_by: MarketSort = "rank",
        limit: int = 50,
        offset: int = 0,
    ) -> ESPNMarketResp:
        season = season or settings.nba_season
        snapshot = _snapshot_date(DraftMarket, season, as_of)
        players = []
        total = 0
        if snapshot is not None:
            query = DraftMarket.select(DraftMarket, Player).join(Player).where(
                (DraftMarket.season == season) & (DraftMarket.source == "espn")
                & (DraftMarket.as_of_date == snapshot)
            )
            if name:
                query = query.where(fn.unaccent(Player.name).contains(fn.unaccent(name.strip())))
            total = query.count()
            order = {
                "rank": DraftMarket.overall_rank.asc(nulls="LAST"),
                "adp": DraftMarket.adp.asc(nulls="LAST"),
                "auction_value": DraftMarket.auction_value.desc(nulls="LAST"),
                "auction_value_avg": DraftMarket.auction_value_avg.desc(nulls="LAST"),
            }[sort_by]
            for row in query.order_by(order, DraftMarket.player).limit(limit).offset(offset):
                players.append(ESPNMarketPlayer(
                    player_id=row.player_id, espn_id=row.player.espn_id, name=row.player.name,
                    **{key: getattr(row, key) for key in (
                        "overall_rank", "adp", "auction_value", "auction_value_avg",
                        "default_position_id", "eligible_slot_ids", "injury_status",
                    )},
                ))
        return ESPNMarketResp(
            status=ApiStatus.SUCCESS,
            message="ESPN draft market snapshot" if snapshot else f"No ESPN market snapshot for {season}",
            data=ESPNMarketData(
                season=season, as_of_date=snapshot, players=players, total=total,
                limit=limit, offset=offset, sort_by=sort_by,
            ),
        )

    @staticmethod
    @db_operation("players.projections")
    def get_projections(
        season: str | None = None,
        as_of: date | None = None,
        name: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> PlayerProjectionsResp:
        season = season or settings.nba_season
        snapshot = _snapshot_date(PlayerProjection, season, as_of)
        players = []
        total = 0
        if snapshot is not None:
            query = PlayerProjection.select(PlayerProjection, Player).join(Player).where(
                (PlayerProjection.season == season)
                & (PlayerProjection.source == "espn")
                & (PlayerProjection.as_of_date == snapshot)
            )
            if name:
                query = query.where(fn.unaccent(Player.name).contains(fn.unaccent(name.strip())))
            total = query.count()
            rows = query.order_by(Player.name, Player.id).limit(limit).offset(offset)
            players = [
                _projection_data(row, row.player, season, snapshot)
                for row in rows
            ]
        return PlayerProjectionsResp(
            status=ApiStatus.SUCCESS,
            message="ESPN per-game projection snapshot"
            if snapshot
            else f"No ESPN projection snapshot for {season}",
            data=PlayerProjectionsData(
                season=season,
                as_of_date=snapshot,
                players=players,
                total=total,
                limit=limit,
                offset=offset,
            ),
        )

    @staticmethod
    @db_operation("market.espn.movement")
    def get_market_movement(
        from_as_of: date,
        to_as_of: date,
        season: str | None = None,
        name: str | None = None,
        metric: MarketSort = "rank",
        direction: MarketMovementDirection = "both",
        limit: int = 50,
        offset: int = 0,
    ) -> ESPNMarketMovementResp:
        season = season or settings.nba_season
        from_snapshot = _snapshot_date(DraftMarket, season, from_as_of)
        to_snapshot = _snapshot_date(DraftMarket, season, to_as_of)
        response_data = {
            "season": season,
            "from_as_of_date": from_snapshot,
            "to_as_of_date": to_snapshot,
            "metric": metric,
            "direction": direction,
            "limit": limit,
            "offset": offset,
        }
        if from_snapshot is None or to_snapshot is None or from_snapshot == to_snapshot:
            message = (
                f"One or both ESPN market snapshot selectors could not be resolved for {season}"
                if from_snapshot is None or to_snapshot is None
                else f"Both selectors resolve to {from_snapshot}; two distinct ESPN market snapshots are required"
            )
            return ESPNMarketMovementResp(
                status=ApiStatus.SUCCESS,
                message=message,
                data=ESPNMarketMovementData(
                    **response_data,
                    players=[],
                    total=0,
                ),
            )

        # Filter each alias before the full outer join. This retains entrants and
        # exits without converting an absent row (or a null measurement) to zero.
        sql = """
            SELECT
                COALESCE(prior.player_id, later.player_id) AS player_id,
                players.espn_id,
                players.name,
                prior.player_id IS NOT NULL AS before_present,
                prior.overall_rank AS before_overall_rank,
                prior.adp AS before_adp,
                prior.auction_value AS before_auction_value,
                prior.auction_value_avg AS before_auction_value_avg,
                later.player_id IS NOT NULL AS after_present,
                later.overall_rank AS after_overall_rank,
                later.adp AS after_adp,
                later.auction_value AS after_auction_value,
                later.auction_value_avg AS after_auction_value_avg
            FROM (
                SELECT player_id, overall_rank, adp, auction_value, auction_value_avg
                FROM nba.draft_market
                WHERE season = %s AND source = 'espn' AND as_of_date = %s
            ) AS prior
            FULL OUTER JOIN (
                SELECT player_id, overall_rank, adp, auction_value, auction_value_avg
                FROM nba.draft_market
                WHERE season = %s AND source = 'espn' AND as_of_date = %s
            ) AS later ON later.player_id = prior.player_id
            JOIN nba.players AS players
              ON players.id = COALESCE(prior.player_id, later.player_id)
        """
        params = [season, from_snapshot, season, to_snapshot]
        if name:
            sql += " WHERE unaccent(players.name) ILIKE '%%' || unaccent(%s) || '%%'"
            params.append(name.strip())

        cursor = DraftMarket._meta.database.execute_sql(sql, params)
        columns = [column[0] for column in cursor.description]
        rows = [dict(zip(columns, values)) for values in cursor.fetchall()]
        movement = []
        metric_field = "overall_rank" if metric == "rank" else metric
        for row in rows:
            before = _market_measurements(row, "before")
            after = _market_measurements(row, "after")
            changes = MarketChanges(
                overall_rank=_change(
                    _movement_value(row, "before", "overall_rank"),
                    _movement_value(row, "after", "overall_rank"),
                    lower_is_better=True,
                ),
                adp=_change(
                    _movement_value(row, "before", "adp"),
                    _movement_value(row, "after", "adp"),
                    lower_is_better=True,
                ),
                auction_value=_change(
                    _movement_value(row, "before", "auction_value"),
                    _movement_value(row, "after", "auction_value"),
                ),
                auction_value_avg=_change(
                    _movement_value(row, "before", "auction_value_avg"),
                    _movement_value(row, "after", "auction_value_avg"),
                ),
            )
            selected_change = getattr(changes, metric_field)
            if direction == "up" and (selected_change is None or selected_change <= 0):
                continue
            if direction == "down" and (selected_change is None or selected_change >= 0):
                continue
            movement.append((selected_change, ESPNMarketMovementPlayer(
                player_id=row["player_id"],
                espn_id=row["espn_id"],
                name=row["name"],
                before=before,
                after=after,
                changes=changes,
            )))

        known = [item for item in movement if item[0] is not None]
        unknown = [item for item in movement if item[0] is None]
        if direction == "up":
            known.sort(key=lambda item: (-item[0], item[1].player_id))
        elif direction == "down":
            known.sort(key=lambda item: (item[0], item[1].player_id))
        else:
            known.sort(key=lambda item: (-abs(item[0]), item[1].player_id))
        unknown.sort(key=lambda item: item[1].player_id)
        ordered = [item[1] for item in known + unknown]
        total = len(ordered)

        return ESPNMarketMovementResp(
            status=ApiStatus.SUCCESS,
            message="ESPN draft market movement",
            data=ESPNMarketMovementData(
                **response_data,
                players=ordered[offset:offset + limit],
                total=total,
            ),
        )

    @staticmethod
    @db_operation("players.projection")
    def get_projection(player_id: int, season: str | None = None, as_of: date | None = None) -> PlayerProjectionResp:
        player = Player.get_or_none(Player.id == player_id)
        if player is None:
            raise NotFoundError("PLAYER_NOT_FOUND", "Player not found")
        season = season or settings.nba_season
        snapshot = _snapshot_date(PlayerProjection, season, as_of)
        # Pick the season's snapshot before filtering to this player. A player
        # omitted from a new batch must not silently retain an older projection.
        row = None if snapshot is None else PlayerProjection.get_or_none(
            (PlayerProjection.player == player_id) & (PlayerProjection.season == season)
            & (PlayerProjection.source == "espn") & (PlayerProjection.as_of_date == snapshot)
        )
        if row is None:
            return PlayerProjectionResp(
                status=ApiStatus.SUCCESS,
                message=f"No ESPN projection for this player in the {season} snapshot"
                + (f" dated {snapshot}" if snapshot else ""),
                data=None,
            )

        return PlayerProjectionResp(
            status=ApiStatus.SUCCESS,
            message="ESPN per-game projection",
            data=_projection_data(row, player, season, snapshot),
        )
