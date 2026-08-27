"""
League settings: detect a league's scoring format from its provider and keep usr.leagues in sync.

Credentials never live here — they stay in usr.teams.league_info. A league row is
identified by (provider, provider_league_id, season). Sync never raises: a provider
failure leaves (or creates) a row with settings_synced_at = NULL, and every consumer
falls back to the default points scoring.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from core.logging import get_logger
from db.models.leagues import League
from db.models.teams import Team
from schemas.common import FantasyProvider, LeagueInfo
from schemas.league import CategoryDefResp, LeagueDetail, LeagueSummary
from services.scoring.models import CategoryDef, LeagueSettings
from services.scoring.providers.espn_settings import parse_espn_settings
from services.scoring.vocab import DEFAULT_CATEGORIES
from services.scoring.providers.yahoo_settings import fetch_yahoo_league_settings, parse_yahoo_settings
from services.providers.http import provider_get
from utils.constants import ESPN_FANTASY_ENDPOINT

log = get_logger()


class LeagueService:

    # ---- identity ------------------------------------------------------------

    @staticmethod
    def provider_league_key(league_info: LeagueInfo) -> tuple[str, str, int]:
        provider = league_info.provider.value if hasattr(league_info.provider, "value") else str(league_info.provider)
        if provider == FantasyProvider.YAHOO.value and league_info.yahoo_team_key:
            return provider, league_info.yahoo_team_key.rsplit(".t.", 1)[0], int(league_info.year)
        return provider, str(league_info.league_id), int(league_info.year)

    @staticmethod
    def get_league_for_league_info(league_info: LeagueInfo) -> Optional[League]:
        provider, pid, season = LeagueService.provider_league_key(league_info)
        return League.get_or_none(
            (League.provider == provider) & (League.provider_league_id == pid) & (League.season == season)
        )

    # ---- provider fetch ------------------------------------------------------

    @staticmethod
    async def fetch_settings(league_info: LeagueInfo, team_id: Optional[int] = None,
                             espn_payload: Optional[dict] = None) -> Optional[LeagueSettings]:
        provider, _, season = LeagueService.provider_league_key(league_info)
        try:
            if provider == FantasyProvider.YAHOO.value:
                from services.yahoo_service import YahooService
                token = await YahooService._ensure_valid_token(league_info, team_id)
                league_key = league_info.yahoo_team_key.rsplit(".t.", 1)[0]
                return parse_yahoo_settings(fetch_yahoo_league_settings(token, league_key), season=season)

            payload = espn_payload
            if payload is None:
                payload = provider_get(
                    "espn",
                    ESPN_FANTASY_ENDPOINT.format(league_info.year, league_info.league_id),
                    params={"view": "mSettings"},
                    cookies={"espn_s2": league_info.espn_s2, "SWID": league_info.swid},
                    expect_key="settings",
                )
            parsed = parse_espn_settings(payload)
            if not parsed.provider_league_id:
                parsed.provider_league_id = str(league_info.league_id)
            if not parsed.season:
                parsed.season = int(league_info.year)
            return parsed
        except Exception as exc:  # provider outages (typed AppErrors included) must never break team flows
            log.warning("league_settings_fetch_failed", provider=provider, team_id=team_id,
                        error=str(exc), error_code=getattr(exc, "error_code", None))
            return None

    # ---- persistence ---------------------------------------------------------

    @staticmethod
    def _stub_settings(league_info: LeagueInfo) -> LeagueSettings:
        provider, pid, season = LeagueService.provider_league_key(league_info)
        return LeagueSettings(provider=provider, provider_league_id=pid, season=season,
                              name=league_info.league_name if league_info.league_name != "N/A" else None,
                              scoring_type="points", category_win_mode=None)

    @staticmethod
    def upsert_league(parsed: LeagueSettings, synced: bool = True) -> League:
        league, created = League.get_or_create(
            provider=parsed.provider, provider_league_id=parsed.provider_league_id, season=parsed.season,
            defaults={"name": parsed.name},
        )
        if created and not synced:
            return league
        if not synced:
            return league   # keep whatever settings the row already has
        now = datetime.utcnow()
        league.name = parsed.name or league.name
        league.scoring_type = parsed.scoring_type
        league.category_win_mode = parsed.category_win_mode
        league.categories = [c.to_json() for c in parsed.categories]
        league.point_weights = parsed.point_weights
        league.matchup_periods = parsed.matchup_periods
        league.roster_slots = parsed.roster_slots
        league.raw_settings = {**(parsed.raw_settings or {}),
                               "_sync": {"unsupported": parsed.unsupported, "warnings": parsed.warnings}}
        league.settings_synced_at = now
        league.updated_at = now
        league.save()
        return league

    @staticmethod
    async def sync_for_team(team: Team, league_info: LeagueInfo,
                            espn_payload: Optional[dict] = None) -> Optional[League]:
        """Fetch + upsert the team's league settings and link the team. Never raises."""
        try:
            parsed = await LeagueService.fetch_settings(league_info, team_id=team.team_id, espn_payload=espn_payload)
            if parsed is not None:
                league = LeagueService.upsert_league(parsed, synced=True)
            else:
                league = LeagueService.upsert_league(LeagueService._stub_settings(league_info), synced=False)
            if team.league_id != league.id:
                Team.update(league=league).where(Team.team_id == team.team_id).execute()
                team.league = league
            log.info("league_synced", team_id=team.team_id, league_id=league.id,
                     scoring_type=league.scoring_type, synced=league.settings_synced_at is not None)
            return league
        except Exception as exc:
            log.error("league_sync_failed", team_id=team.team_id, error=str(exc))
            return None

    # ---- response shaping ----------------------------------------------------

    @staticmethod
    def to_summary(league: Optional[League]) -> Optional[LeagueSummary]:
        if league is None:
            return None
        return LeagueSummary(
            id=league.id, provider=league.provider, provider_league_id=league.provider_league_id,
            season=league.season, name=league.name, scoring_type=league.scoring_type,
            category_win_mode=league.category_win_mode,
            categories=[CategoryDefResp(**c) for c in (league.categories or [])],
            point_weights=dict(league.point_weights or {}),
            settings_synced=league.settings_synced_at is not None,
            settings_synced_at=league.settings_synced_at.isoformat() if league.settings_synced_at else None,
        )

    @staticmethod
    def preview_of(league_info_json: Optional[str]) -> Optional[str]:
        """A team's `scoring_preview` straight from its stored league_info JSON.

        Tolerant of legacy/partial rows: anything that isn't a recognised value
        is None, and no full LeagueInfo validation is required.
        """
        if not league_info_json:
            return None
        try:
            value = json.loads(league_info_json).get("scoring_preview")
        except (ValueError, AttributeError):
            return None
        return value if value in ("points", "categories") else None

    @staticmethod
    def apply_preview(summary: Optional[LeagueSummary], preview: Optional[str],
                      league_info: Optional[LeagueInfo] = None) -> Optional[LeagueSummary]:
        """Overlay a team's `scoring_preview` on the league summary the UI renders from.

        Mirrors `resolve_scoring(league, preview)`: categories preview uses the
        league's own categories when it has them, else the standard 9-cat; points
        preview keeps the league's weights. A preview on a team with no league
        row yet gets a synthetic summary (when league_info is available) so the
        UI can still switch format.
        """
        if preview not in ("points", "categories"):
            return summary

        if summary is None:
            if league_info is None:
                return None
            provider, pid, season = LeagueService.provider_league_key(league_info)
            summary = LeagueSummary(
                id=0, provider=provider, provider_league_id=pid, season=season,
                name=league_info.league_name, scoring_type="points",
            )

        if preview == "categories":
            if summary.scoring_type != "categories" or not summary.categories:
                summary.categories = [
                    CategoryDefResp(**CategoryDef.for_key(k).to_json()) for k in DEFAULT_CATEGORIES
                ]
                summary.category_win_mode = summary.category_win_mode or "each_category"
            summary.scoring_type = "categories"
        else:
            summary.scoring_type = "points"
            summary.categories = []
            summary.category_win_mode = None
        summary.settings_synced = True
        summary.scoring_preview = preview
        return summary

    @staticmethod
    def summary_for_team(league: Optional[League], league_info: Optional[LeagueInfo]) -> Optional[LeagueSummary]:
        preview = getattr(league_info, "scoring_preview", None) if league_info is not None else None
        return LeagueService.apply_preview(LeagueService.to_summary(league), preview, league_info)

    @staticmethod
    def to_detail(league: League, preview: Optional[str] = None) -> LeagueDetail:
        summary = LeagueService.apply_preview(LeagueService.to_summary(league), preview)
        sync = (league.raw_settings or {}).get("_sync", {}) if isinstance(league.raw_settings, dict) else {}
        return LeagueDetail(
            **summary.model_dump(),
            matchup_periods=dict(league.matchup_periods or {}),
            roster_slots=dict(league.roster_slots or {}),
            unsupported=list(sync.get("unsupported", [])),
            warnings=list(sync.get("warnings", [])),
        )

    @staticmethod
    def league_info_of(team: Team) -> LeagueInfo:
        from services.team_service import TeamService
        # Credential path: this feeds provider calls, so the secrets must be
        # merged in from the encrypted store when the team has been migrated.
        return TeamService.deserialize_league_info(json.loads(team.league_info), team)
