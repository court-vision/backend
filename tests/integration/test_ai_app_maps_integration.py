"""
Integration: the app map's storage against the real schema (migration 0039).

One row per build, replaced by a later crawl of it unless that crawl couldn't
sign in (an older crawl that finishes last never replaces it); maps over 30 days old pruned at each upload with the newest kept; and
loading a build's map, or the newest when the build has none yet.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from db.base import db
from db.models.ai_app_maps import AiAppMap
from schemas.app_map import AppMapUpload
from services.ai import app_map

pytestmark = [pytest.mark.integration]

A = "a" * 40
B = "b" * 40


@pytest.fixture(autouse=True)
def empty(integration_db):
    db.execute_sql("TRUNCATE usr.ai_app_maps")
    app_map._cache.clear()
    yield
    db.execute_sql("TRUNCATE usr.ai_app_maps")
    app_map._cache.clear()


def raw(build, crawled_at, signed_in=True, note="Your team's matchup week"):
    return {
        "version": 1, "build": build, "crawledAt": crawled_at.isoformat(),
        "desks": [{"id": "week", "label": "Week", "href": "/week", "note": note, "keywords": [], "needs": "account"}],
        "screens": [{"id": "week", "desk": "week", "title": "Week", "trail": [], "url": "/week", "link": "/week",
                     "tree": "- button \"Market P\"", "signedIn": signed_in, "fingerprint": "x"}],
        "copy": [], "findings": [],
    }


def store(body):
    return asyncio.run(app_map.store(AppMapUpload.model_validate(body), body, 100))


def load(build):
    return asyncio.run(app_map.load(build))


NOW = datetime.now(timezone.utc).replace(microsecond=0)


def test_a_map_is_stored_as_uploaded():
    body = raw(A, NOW)
    kept, stats, pruned = store(body)
    assert (kept, pruned) == (None, 0)
    row = AiAppMap.get_by_id(A)
    assert row.map == body
    assert row.crawled_at == NOW
    assert row.stats == {"screens": 1, "signed_in_screens": 1, "desks": 1, "unseen_copy": 0, "findings": 0, "bytes": 100}


def test_a_later_crawl_of_the_build_replaces_it():
    store(raw(A, NOW, note="before"))
    store(raw(A, NOW + timedelta(hours=12), note="after"))
    assert AiAppMap.select().count() == 1
    assert AiAppMap.get_by_id(A).map["desks"][0]["note"] == "after"


def test_an_older_crawl_that_finishes_last_keeps_the_newer_map():
    store(raw(A, NOW + timedelta(hours=12), note="newer"))
    kept, _, _ = store(raw(A, NOW, note="older"))
    assert kept == "Kept the newer map of this build"
    row = AiAppMap.get_by_id(A)
    assert (row.map["desks"][0]["note"], row.crawled_at) == ("newer", NOW + timedelta(hours=12))


def test_a_crawl_that_could_not_sign_in_keeps_the_signed_in_map():
    store(raw(A, NOW, note="signed in"))
    kept, _, _ = store(raw(A, NOW + timedelta(hours=12), signed_in=False, note="signed out"))
    assert kept == "Kept the signed-in map of this build"
    assert AiAppMap.get_by_id(A).map["desks"][0]["note"] == "signed in"
    # A signed-out map is replaced by anything
    store(raw(B, NOW, signed_in=False, note="signed out"))
    assert store(raw(B, NOW, signed_in=False, note="again"))[0] is None


def test_old_maps_are_pruned_but_never_the_newest():
    store(raw(A, NOW - timedelta(days=45)))
    # The only map is the newest: kept, however old
    assert store(raw(A, NOW - timedelta(days=45)))[2] == 0
    _, _, pruned = store(raw(B, NOW))
    assert pruned == 1
    assert [m.build for m in AiAppMap.select()] == [B]


def test_load_gives_the_build_or_else_the_newest():
    assert load(A) is None
    store(raw(A, NOW - timedelta(days=1), note="older"))
    store(raw(B, NOW, note="newer"))
    assert load(A).build == A
    assert load(A).find("market", requested_build=A)["matches_build"] is True
    fallback = load("c" * 40)
    assert fallback.build == B
    assert fallback.find("market", requested_build="c" * 40)["matches_build"] is False
    assert load(None).build == B


def test_a_new_upload_of_a_cached_build_is_read_fresh():
    store(raw(A, NOW, note="first"))
    first = load(A)
    assert first.map.desks[0].note == "first"
    assert load(A) is first  # cached while it is the current upload
    # Another worker stores the build's next crawl: this one's cache can't know, the upload time tells it
    (AiAppMap.update(map=raw(A, NOW, note="second"), uploaded_at=AiAppMap.uploaded_at + timedelta(seconds=1))
     .where(AiAppMap.build == A).execute())
    assert load(A).map.desks[0].note == "second"
