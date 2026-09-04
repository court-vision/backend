from datetime import datetime

from peewee import (
    AutoField,
    BigIntegerField,
    BooleanField,
    CharField,
    DateTimeField,
    DecimalField,
    ForeignKeyField,
    IntegerField,
)
from playhouse.postgres_ext import BinaryJSONField

from db.base import BaseModel
from db.models.leagues import League
from db.models.nba.players import Player
from db.models.teams import Team
from db.models.users import User


class DraftSession(BaseModel):
    """One draft-room session. The board itself is derived state and never stored;
    a session plus its picks is the complete record of a draft."""

    id = AutoField()
    user = ForeignKeyField(User, column_name="user_id", backref="draft_sessions")
    team = ForeignKeyField(Team, column_name="team_id", null=True, backref="draft_sessions")   # null for mock drafts
    league = ForeignKeyField(League, column_name="league_id", null=True, backref="draft_sessions")
    kind = CharField(max_length=16, default="manual")       # live | manual | mock | import
    status = CharField(max_length=16, default="active")     # active | completed | abandoned
    name = CharField(max_length=80, null=True)              # the user's own label
    espn_league_id = BigIntegerField(null=True)             # the ESPN draft this room follows (live: its league; mock: learned on INIT)
    draft_type = CharField(max_length=16, default="snake")  # snake | auction
    pick_order = BinaryJSONField(default=list)              # provider team ids in first-round order
    my_slot = IntegerField(null=True)                       # 1-based slot of the user's team
    rounds = IntegerField(null=True)
    keepers = BinaryJSONField(default=list)                 # pre-designated keepers [{player_id?, espn_player_id?, name, slot?}]
    started_at = DateTimeField(null=True)
    completed_at = DateTimeField(null=True)
    created_at = DateTimeField(default=datetime.utcnow)
    updated_at = DateTimeField(default=datetime.utcnow)

    class Meta:
        table_name = "draft_sessions"
        schema = "usr"

    def __repr__(self):
        return (
            f"<DraftSession(id={self.id}, user_id={self.user_id}, kind='{self.kind}', "
            f"status='{self.status}', draft_type='{self.draft_type}')>"
        )


class DraftPick(BaseModel):
    """One pick in a session. `player` may lag resolution (sync/import can see an
    ESPN id or name before the NBA-id lookup lands), so the provider identity
    rides along."""

    id = AutoField()
    session = ForeignKeyField(DraftSession, column_name="session_id", backref="picks")
    overall_pick = IntegerField()
    round = IntegerField(null=True)                         # null for auction drafts
    slot = IntegerField(null=True)                          # drafting team's slot in pick_order
    player = ForeignKeyField(Player, column_name="player_id", null=True, backref="draft_picks")
    espn_player_id = IntegerField(null=True)
    espn_team_id = BigIntegerField(null=True)               # who ESPN says picked (an id from pick_order); null for a hand-entered pick
    player_name = CharField(max_length=255, null=True)
    by_me = BooleanField(default=False)
    source = CharField(max_length=16, default="manual")     # manual | espn_sync | import
    bid = DecimalField(max_digits=7, decimal_places=2, null=True)   # auction (v2)
    created_at = DateTimeField(default=datetime.utcnow)

    class Meta:
        table_name = "draft_picks"
        schema = "usr"
        indexes = (
            (("session", "overall_pick"), True),
        )

    def __repr__(self):
        return (
            f"<DraftPick(id={self.id}, session_id={self.session_id}, "
            f"overall_pick={self.overall_pick}, player_name='{self.player_name}')>"
        )


# One ACTIVE room per user per ESPN draft (migration 0016): the bijection the
# room relies on to know which ESPN draft it is linked to. Partial, so a
# finished room does not block next season's draft in the same league.
DraftSession.add_index(
    DraftSession.index(
        DraftSession.user,
        DraftSession.espn_league_id,
        unique=True,
        where=(DraftSession.status == "active") & DraftSession.espn_league_id.is_null(False),
        name="draft_sessions_user_espn_league_active_uq",
    )
)


# One pick per resolved player per session: the race-settling backstop for the
# duplicate check in DraftService.add_pick (migration 0013). Partial on purpose —
# a pick recorded before its player reached nba.players has no player_id and
# carries only a provider identity, which the service compares instead.
DraftPick.add_index(
    DraftPick.index(
        DraftPick.session,
        DraftPick.player,
        unique=True,
        where=DraftPick.player.is_null(False),
        name="draft_picks_session_player_uq",
    )
)
