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

import requests

from core.logging import get_logger
from core.settings import settings
from db.models.leagues import League
from db.models.teams import Team
from schemas.common import FantasyProvider, LeagueInfo
from schemas.league import CategoryDefResp, LeagueDetail, LeagueSummary
from services.scoring.models import LeagueSettings
from services.scoring.providers.espn_settings import parse_espn_settings
from services.scoring.providers.yahoo_settings import fetch_yahoo_league_settings, parse_yahoo_settings
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
                resp = requests.get(
                    ESPN_FANTASY_ENDPOINT.format(league_info.year, league_info.league_id),
                    params={"view": "mSettings"},
                    cookies={"espn_s2": league_info.espn_s2, "SWID": league_info.swid},
                    timeout=settings.http_timeout,
                )
                resp.raise_for_status()
                payload = resp.json()
            parsed = parse_espn_settings(payload)
            if not parsed.provider_league_id:
                parsed.provider_league_id = str(league_info.league_id)
            if not parsed.season:
                parsed.season = int(league_info.year)
            return parsed
        except Exception as exc:  # provider outages must never break team flows
            log.warning("league_settings_fetch_failed", provider=provider, team_id=team_id, error=str(exc))
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
    def to_detail(league: League) -> LeagueDetail:
        summary = LeagueService.to_summary(league)
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
        return TeamService.deserialize_league_info(json.loads(team.league_info))
