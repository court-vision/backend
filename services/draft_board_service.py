"""
Draft Lab board v0: every draftable player, valued by one league's scoring.

Composes what already exists rather than inventing a new engine:

- Pool:   ESPN's published per-game projections (nba.player_projections, latest
          snapshot) where present, union each player's final previous-season row
          (services.scoring.pool.load_baseline_pool) for everyone else. Rookies
          therefore appear exactly when projections do.
- Value:  the same dispatcher math every provider uses — the league's point
          weights for points leagues, the fpts-scale category value
          (services.scoring.category_value) for category leagues.
- Market: ESPN editorial draft rank / auction value and crowd ADP from
          nba.draft_market (latest snapshot), joined by player id, with a
          `market_rank − cv_rank` delta.
- Caps:   hard per-position roster caps from usr.leagues.position_limits mark
          candidates the caller can no longer draft (flagged, never hidden).

cv_rank is computed over the FULL pool, picked players included, so it reads as
a pre-draft big-board rank: it stays stable as picks remove rows and remains
comparable to market_rank all draft long.

One `run_db` fetch materializes every input; one `run_cpu` call scores and
assembles the response (the rankings-service split — z-scoring a pool and
building hundreds of pydantic rows must not hold a DB permit).

Position caps are ESPN-position-keyed (PG/SG/SF/PF/C) while nba.players carries
coarser nba_api positions (G, F, C, hyphenated). A coarse group is enforceable
only when every ESPN position inside it is capped — with only {"PG": 2} an
uncapped SG slot could absorb any guard we cannot tell apart — so caps are
summed per group (G = PG+SG, F = SF+PF, C = C) and players count by their
primary listed position. Exact for the common C-only cap; conservative (never
wrongly blocks) for split caps. ESPN's own counting rule is still open
(docs/DRAFT_LAB_PLAN.md §8 #5); revisit when the spike answers it.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Iterable, Mapping, Optional

from core.compute import run_cpu
from core.settings import settings
from db import base as db_base
from db.models.nba.draft_market import DraftMarket
from db.models.nba.player_projections import PlayerProjection
from db.models.nba.players import Player
from schemas.common import ApiStatus, CategoryDefResp
from schemas.draft import DraftBoardMeta, DraftBoardResp, DraftBoardRow
from services.player_value_service import PlayerValueService
from services.scoring.category_rank import PoolRow, compute_category_scores
from services.scoring.category_value import category_value, rankable_categories
from services.scoring.models import StatLine
from services.scoring.points import DEFAULT_POINTS
from services.scoring.pool import load_baseline_pool

if TYPE_CHECKING:  # pragma: no cover
    from services.scoring.resolver import ResolvedScoring

# ESPN cap position -> the coarse nba_api position group it belongs to, and how
# many ESPN positions each group holds (see the module docstring).
_COARSE_GROUP: dict[str, str] = {"PG": "G", "SG": "G", "SF": "F", "PF": "F", "C": "C"}
_GROUP_SIZE: dict[str, int] = {"G": 2, "F": 2, "C": 1}

VALUE_DECIMALS = 1


@dataclass
class BoardInputs:
    """Everything the board needs from the database, fully materialized."""

    season: str
    pool: list[PoolRow]                                 # one row per player: projection line, else baseline
    source: dict[int, str] = field(default_factory=dict)            # player id -> projection | baseline
    last_season_gp: dict[int, int] = field(default_factory=dict)    # players with a baseline row
    projected_gp: dict[int, Optional[int]] = field(default_factory=dict)
    projections_as_of: Optional[date] = None
    market: dict[int, dict] = field(default_factory=dict)           # player id -> {overall_rank, adp, auction_value}
    market_as_of: Optional[date] = None
    positions: dict[int, Optional[str]] = field(default_factory=dict)


class DraftBoardService:

    @staticmethod
    async def get_board(
        scoring: "ResolvedScoring",
        picked_ids: Iterable[int] = (),
        my_ids: Iterable[int] = (),
    ) -> DraftBoardResp:
        """The board for one league: ranked rows minus everyone already drafted.

        `picked_ids` are NBA player ids drafted by anyone; `my_ids` the subset
        (not required to be repeated in `picked_ids`) drafted by the caller,
        which is what the position-cap check counts against.
        """
        picked, mine = frozenset(picked_ids), frozenset(my_ids)
        inputs = await db_base.run_db("draft_board.fetch", DraftBoardService._fetch_inputs, mine)
        return await run_cpu("draft_board.build", DraftBoardService._build_board, scoring, picked, mine, inputs)

    # ---- one trip to the database ---------------------------------------------

    @staticmethod
    def _fetch_inputs(my_ids: frozenset[int]) -> BoardInputs:
        season = settings.nba_season

        baseline = {row.id: row for row in load_baseline_pool()}
        pool: dict[int, PoolRow] = dict(baseline)
        source = {pid: "baseline" for pid in baseline}
        last_season_gp = {pid: row.gp for pid, row in baseline.items()}

        projections_as_of, projections = DraftBoardService._latest_projections(season)
        projected_gp: dict[int, Optional[int]] = {}
        for rec in projections:
            line = StatLine.from_row(rec)
            gp = int(rec.projected_gp) if rec.projected_gp is not None else 0
            fpts = round(DEFAULT_POINTS.score(line), 1)
            base = baseline.get(rec.player_id)
            pool[rec.player_id] = PoolRow(
                id=rec.player_id, name=rec.player.name, team=(base.team if base else None),
                gp=gp, line=line, fpts_avg=fpts, fpts_total=round(fpts * gp, 1),
                espn_id=rec.player.espn_id, name_normalized=rec.player.name_normalized,
            )
            source[rec.player_id] = "projection"
            projected_gp[rec.player_id] = int(rec.projected_gp) if rec.projected_gp is not None else None

        market: dict[int, dict] = {}
        market_as_of: Optional[date] = None
        for rec in DraftMarket.latest_for_season(season):
            market_as_of = rec.as_of_date
            market[rec.player_id] = {
                "overall_rank": int(rec.overall_rank) if rec.overall_rank is not None else None,
                "adp": round(float(rec.adp), 2) if rec.adp is not None else None,
                "auction_value": float(rec.auction_value) if rec.auction_value is not None else None,
            }

        wanted = set(pool) | set(my_ids)
        positions: dict[int, Optional[str]] = {}
        if wanted:
            for rec in Player.select(Player.id, Player.position).where(Player.id.in_(list(wanted))):
                positions[rec.id] = rec.position

        return BoardInputs(
            season=season, pool=list(pool.values()), source=source,
            last_season_gp=last_season_gp, projected_gp=projected_gp,
            projections_as_of=projections_as_of,
            market=market, market_as_of=market_as_of, positions=positions,
        )

    @staticmethod
    def _latest_projections(season: str, source: str = "espn") -> tuple[Optional[date], list]:
        """Every player's row from the latest projection snapshot, Player joined.

        (The mirrored model's `latest_for_season` returns bare rows; the board
        also needs each player's name/espn_id, so the join lives here.)
        """
        latest = (
            PlayerProjection.select(PlayerProjection.as_of_date)
            .where((PlayerProjection.season == season) & (PlayerProjection.source == source))
            .order_by(PlayerProjection.as_of_date.desc())
            .limit(1)
            .scalar()
        )
        if latest is None:
            return None, []
        records = (
            PlayerProjection.select(PlayerProjection, Player)
            .join(Player)
            .where((PlayerProjection.season == season)
                   & (PlayerProjection.source == source)
                   & (PlayerProjection.as_of_date == latest))
        )
        return latest, list(records)

    # ---- pure assembly ---------------------------------------------------------

    @staticmethod
    def _build_board(
        scoring: "ResolvedScoring",
        picked_ids: frozenset[int],
        my_ids: frozenset[int],
        inputs: BoardInputs,
    ) -> DraftBoardResp:
        cat_defs = rankable_categories(scoring) if scoring.is_categories else []

        # (row, value, per-category values, per-category z, z-sum), best first.
        if scoring.is_categories:
            scored = compute_category_scores(inputs.pool, cat_defs)
            entries = [(s.row, category_value(s.score), s.values, s.z, s.score) for s in scored]
        else:
            points = scoring.points
            entries = [(row, round(points.score(row.line), VALUE_DECIMALS), None, None, None)
                       for row in inputs.pool]
            entries.sort(key=lambda e: (-e[1], -e[0].fpts_avg))

        caps = DraftBoardService._enforceable_caps(DraftBoardService._position_limits(scoring))
        my_counts = Counter(
            group for group in (DraftBoardService._primary_group(inputs.positions.get(pid)) for pid in my_ids)
            if group is not None
        )

        removed = picked_ids | my_ids
        rows: list[DraftBoardRow] = []
        for cv_rank, (row, value, cats, z, z_sum) in enumerate(entries, start=1):
            if row.id in removed:
                continue
            market = inputs.market.get(row.id, {})
            market_rank = market.get("overall_rank")
            group = DraftBoardService._primary_group(inputs.positions.get(row.id))
            rows.append(DraftBoardRow(
                player_id=row.id,
                espn_id=row.espn_id,
                name=row.name,
                team=row.team,
                position=inputs.positions.get(row.id),
                cv_rank=cv_rank,
                value=value,
                value_source=inputs.source.get(row.id, "baseline"),
                last_season_gp=inputs.last_season_gp.get(row.id),
                projected_gp=inputs.projected_gp.get(row.id),
                fpts_avg=row.fpts_avg,
                market_rank=market_rank,
                adp=market.get("adp"),
                auction_value=market.get("auction_value"),
                market_delta=(market_rank - cv_rank) if market_rank is not None else None,
                cap_blocked=(group in caps and my_counts[group] >= caps[group]),
                categories=cats,
                category_z=z,
                score=z_sum,
            ))

        if rows:
            message = f"Draft board fetched successfully ({len(rows)} available of {len(entries)})"
        else:
            message = f"No {inputs.season} player data yet — the board opens on last season's baseline"
        return DraftBoardResp(
            status=ApiStatus.SUCCESS,
            message=message,
            data=rows,
            meta=DraftBoardMeta(
                season=inputs.season,
                format=scoring.format,
                value_kind=PlayerValueService.value_kind_for(scoring),
                pool_size=len(entries),
                available=len(rows),
                projection_count=sum(1 for s in inputs.source.values() if s == "projection"),
                baseline_count=sum(1 for s in inputs.source.values() if s == "baseline"),
                projections_as_of=inputs.projections_as_of,
                market_as_of=inputs.market_as_of,
                position_limits=DraftBoardService._position_limits(scoring),
                categories=[CategoryDefResp(**c.to_json()) for c in cat_defs],
                settings_synced=scoring.settings_synced if scoring.league is not None else None,
            ),
        )

    # ---- position caps ---------------------------------------------------------

    @staticmethod
    def _position_limits(scoring: "ResolvedScoring") -> dict[str, int]:
        limits = getattr(scoring.league, "position_limits", None) if scoring.league is not None else None
        return dict(limits) if limits else {}

    @staticmethod
    def _enforceable_caps(position_limits: Mapping[str, int]) -> dict[str, int]:
        """Caps summed into the coarse groups our position data can enforce.

        A group qualifies only when every ESPN position in it is capped; an
        explicit 0 is a real "none allowed" rule and sums like any other cap.
        """
        groups: dict[str, list[int]] = {}
        for pos, cap in (position_limits or {}).items():
            group = _COARSE_GROUP.get(str(pos).upper())
            if group is None:
                continue
            try:
                groups.setdefault(group, []).append(int(cap))
            except (TypeError, ValueError):
                continue
        return {g: sum(caps) for g, caps in groups.items() if len(caps) == _GROUP_SIZE[g]}

    @staticmethod
    def _primary_group(position: Optional[str]) -> Optional[str]:
        """A player's primary coarse position: the first segment of 'F-C' is F."""
        if not position:
            return None
        head = position.split("-", 1)[0].strip().upper()
        return head if head in _GROUP_SIZE else None
