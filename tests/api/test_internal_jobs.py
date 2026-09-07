"""
/v1/internal/jobs/lineup/evaluate: the pipeline-token route. No Clerk user — a
missing or wrong bearer is refused, the right one reaches the (stubbed) service.
"""

import pytest

from core import pipeline_auth
from schemas.common import ApiStatus
from schemas.lineup_editor import LineupEvaluationData, LineupEvaluationResp
from services import lineup_editor_service as svc

BODY = {"team_id": 21, "user_id": 11, "nba_date": "2026-10-20", "apply": False}


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(pipeline_auth, "PIPELINE_API_TOKEN", "pipe-token")


@pytest.mark.api
def test_missing_bearer_is_refused(client, token):
    assert client.post("/v1/internal/jobs/lineup/evaluate", json=BODY).status_code in (401, 403)


@pytest.mark.api
def test_wrong_bearer_is_401(client, token):
    r = client.post("/v1/internal/jobs/lineup/evaluate", json=BODY, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


@pytest.mark.api
def test_right_bearer_reaches_the_service(client, token, monkeypatch):
    seen = []

    async def fake(req):
        seen.append(req)
        return LineupEvaluationResp(status=ApiStatus.SUCCESS, message="Lineup planned",
                                    data=LineupEvaluationData(outcome="planned", team_name="T"))

    monkeypatch.setattr(svc.LineupEditorService, "evaluate", staticmethod(fake))
    r = client.post("/v1/internal/jobs/lineup/evaluate", json=BODY, headers={"Authorization": "Bearer pipe-token"})
    assert r.status_code == 200 and r.json()["data"]["outcome"] == "planned"
    assert seen[0].team_id == 21 and seen[0].apply is False and str(seen[0].nba_date) == "2026-10-20"


@pytest.mark.api
def test_unconfigured_token_is_a_500(client, monkeypatch):
    monkeypatch.setattr(pipeline_auth, "PIPELINE_API_TOKEN", None)
    r = client.post("/v1/internal/jobs/lineup/evaluate", json=BODY, headers={"Authorization": "Bearer x"})
    assert r.status_code == 500
