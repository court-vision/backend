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
    ESPNMarketData, ESPNMarketPlayer, ESPNMarketResp, MarketSort,
    PlayerProjectionData, PlayerProjectionResp, ProjectionStats,
)


def _snapshot_date(model, season: str, as_of: date | None = None):
    query = model.select(fn.MAX(model.as_of_date)).where(
        (model.season == season) & (model.source == "espn")
    )
    if as_of is not None:
        query = query.where(model.as_of_date <= as_of)
    return query.scalar()


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

        stats = {key: getattr(row, key) for key in PlayerProjection.STAT_KEYS}
        for rate, made, attempted in (("fg_pct", "fgm", "fga"), ("fg3_pct", "fg3m", "fg3a"), ("ft_pct", "ftm", "fta")):
            stats[rate] = (
                round(float(stats[made] / stats[attempted]), 4)
                if stats[made] is not None and stats[attempted] is not None and stats[attempted] > 0
                else None
            )
        return PlayerProjectionResp(
            status=ApiStatus.SUCCESS,
            message="ESPN per-game projection",
            data=PlayerProjectionData(
                player_id=player.id, espn_id=player.espn_id, name=player.name,
                season=season, as_of_date=snapshot, projected_gp=row.projected_gp,
                stats=ProjectionStats(**stats),
            ),
        )
