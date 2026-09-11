"""
Provider connections as the client sees them: which accounts are connected,
whether their credentials still work, which teams use them, and -- for ESPN --
which teams the account has, as ESPN lists them.

Never the credentials -- not even the whole SWID, which `credential_service`
treats as a secret. `account_hint` carries its last four characters, enough to
tell two ESPN accounts apart.
"""

from datetime import datetime
from typing import Literal, Optional

from pydantic import Field

from schemas.common import ApiModel, BaseRequest, BaseResponse, FantasyProvider


class ConnectionTeamInfo(ApiModel):
    team_id: int
    team_name: str
    league_name: Optional[str] = None
    league_id: Optional[int] = None
    year: Optional[int] = None


class ProviderConnectionInfo(ApiModel):
    id: int
    provider: FantasyProvider
    # "…E5F6" for ESPN; None for Yahoo, whose rows carry no account id
    account_hint: Optional[str] = None
    # ok: the provider accepted the credentials at the last check. expired: it
    # has rejected them since. unknown: not checked since they were saved.
    status: Literal["ok", "expired", "unknown"]
    verified_at: Optional[datetime] = None
    auth_failed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    # The teams whose provider calls use this connection's credentials
    teams: list[ConnectionTeamInfo] = []


class EspnAccountTeam(ApiModel):
    league_id: int
    season: int
    # ESPN's id for the team inside its league
    espn_team_id: int
    team_name: str
    team_abbrev: Optional[str] = None
    league_name: Optional[str] = None
    league_size: Optional[int] = None
    # ESPN's name for the format: H2H_POINTS, H2H_CATEGORY, ROTO, ...
    scoring_type: Optional[str] = None
    # The Court Vision team already tracking this one, if any
    tracked_team_id: Optional[int] = None


class EspnConnectReq(BaseRequest):
    espn_s2: str = Field(min_length=1)
    swid: str = Field(min_length=1)


class ProviderConnectionListResp(BaseResponse):
    data: list[ProviderConnectionInfo] = []


class ProviderConnectionResp(BaseResponse):
    data: Optional[ProviderConnectionInfo] = None
    # True when this call created the connection rather than refreshing one
    created: bool = False


class EspnAccountTeamsResp(BaseResponse):
    data: list[EspnAccountTeam] = []


class ProviderConnectionDeleteData(ApiModel):
    id: int
    unlinked_team_ids: list[int] = []


class ProviderConnectionDeleteResp(BaseResponse):
    data: Optional[ProviderConnectionDeleteData] = None
