"""
The app map: CV AI's map of the website, one per crawled frontend build
(docs/APP_MAP_PLAN.md § 4, § 5).

The frontend's App Map workflow crawls every commit on main and uploads what it
found; nobody writes the map by hand. This module stores an upload, loads the
map of the build a user has open (or the newest, when that build's crawl hasn't
landed yet), and searches it: `find` is what the agent's `find_in_app` tool
returns.

Storing:
- one row per build, replaced by a later crawl of the same build (the nightly
  crawl during games sees more), except that a crawl that couldn't sign in
  never replaces one that did: it would only lose the account's screens;
- maps crawled more than 30 days ago are pruned at each upload, the newest
  always kept.

Searching is BM25 over each screen's trail (weighted most), its desk's own
words, and the app's words in its tree: the tokens of the tree that also occur
in the source's copy, so a player's or a team's name in the crawled data
doesn't make a screen look relevant. The copy no crawled screen showed
(`unseen`) is searched apart and returned as such. Pure Python, in memory, a
few hundred screens at most; the parsed map and its index are cached by build
and upload.
"""

from __future__ import annotations

import math
import re
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from core.logging import get_logger
from db.base import db, db_operation
from db.models.ai_app_maps import AiAppMap
from schemas.app_map import AppMapStats, AppMapUpload, MapScreen

log = get_logger("app_map")

RETENTION = timedelta(days=30)
# Builds whose parsed map and index stay in memory: the one users have open, the
# one before it in tabs left open, and the newest while a deploy's crawl runs
CACHED_BUILDS = 3

# What one `find` returns: the best screens, each tree cut to a size that keeps
# the whole result near one table lookup, and the best copy no screen showed
SCREENS = 5
TREE_CHARS = 1_200
UNSEEN = 3

# How much more a word in the trail counts than one in the tree or the desk's words
TRAIL_WEIGHT = 3.0
K1 = 1.2
B = 0.75

_STOP = frozenset(
    "a an and are at be by can do does for from how i in is it me my of on or "
    "the this that to what where which with you your".split()
)


def tokens(text: str) -> list[str]:
    """Lowercase words, stop words out, plurals folded ("players" → "player")."""
    out = []
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if w in _STOP:
            continue
        if len(w) > 4 and w.endswith("ies"):
            w = w[:-3] + "y"
        elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out


class _Bm25:
    """Okapi BM25 over documents given as weighted term counts."""

    def __init__(self, docs: list[Counter]):
        self.docs = docs
        self.lengths = [sum(d.values()) for d in docs]
        self.avg = (sum(self.lengths) / len(docs)) if docs else 0.0
        df: Counter = Counter()
        for d in docs:
            df.update(d.keys())
        n = len(docs)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def scores(self, query: list[str]) -> list[float]:
        terms = [t for t in dict.fromkeys(query) if t in self.idf]
        out = []
        for doc, length in zip(self.docs, self.lengths):
            s = 0.0
            for t in terms:
                tf = doc.get(t, 0.0)
                if tf:
                    s += self.idf[t] * tf * (K1 + 1) / (tf + K1 * (1 - B + B * length / (self.avg or 1)))
            out.append(s)
        return out


def _step_words(step) -> str:
    # A step named by data ("Move Tyrese Maxey") is searched by where it sits
    return f"{step.within or ''} {step.role}" if step.example else step.name


def _steps(desk_label: str, screen: MapScreen) -> str:
    """ "Week › Market P › ONE DAY": the clicks from the desk's front, as a user reads them."""
    parts = [desk_label]
    for step in screen.trail:
        parts.append(f"{step.name} (one of a list)" if step.example else step.name)
    return " › ".join(parts)


def _cut(tree: str, limit: int = TREE_CHARS) -> str:
    if len(tree) <= limit:
        return tree
    head = tree[:limit]
    return head[: head.rfind("\n")] + "\n- …" if "\n" in head else head + "…"


@dataclass
class LoadedMap:
    """One build's map, parsed, with its search index."""

    build: str
    crawled_at: datetime
    map: AppMapUpload
    # Which upload of the build this is: a later crawl of it replaces the row
    uploaded_at: Optional[datetime] = None
    _screens: _Bm25 = field(init=False, repr=False)
    _unseen: list[tuple[str, Optional[str], str]] = field(init=False, repr=False)
    _unseen_index: _Bm25 = field(init=False, repr=False)

    def __post_init__(self):
        m = self.map
        desks = {d.id: d for d in m.desks}
        # The app's own words: everything its source writes for users, its desks' words, its controls' names
        vocab: set[str] = set()
        for c in m.copy_:
            for s in (*c.seen, *c.unseen):
                vocab.update(tokens(s))
        for d in m.desks:
            vocab.update(tokens(f"{d.label} {d.note} {' '.join(d.keywords)}"))
        for s in m.screens:
            for step in s.trail:
                vocab.update(tokens(_step_words(step)))

        docs = []
        for s in m.screens:
            desk = desks.get(s.desk)
            doc: Counter = Counter()
            trail = " ".join(_step_words(step) for step in s.trail) or (desk.label if desk else s.desk)
            for t in tokens(trail):
                doc[t] += TRAIL_WEIGHT
            if desk:
                doc.update(tokens(f"{desk.label} {desk.note} {' '.join(desk.keywords)}"))
            doc.update(t for t in tokens(s.tree) if t in vocab)
            docs.append(doc)
        self._screens = _Bm25(docs)

        self._unseen = [(text, c.desk, c.file) for c in m.copy_ for text in c.unseen]
        self._unseen_index = _Bm25([Counter(tokens(text)) for text, _, _ in self._unseen])

    def outline(self) -> list[dict[str, Any]]:
        """Every desk with its note, keys and the views a link opens: the model's bearings."""
        out = []
        for d in self.map.desks:
            views = [s.title for s in self.map.screens if s.desk == d.id and len(s.trail) == 1 and s.link]
            out.append({"id": d.id, "label": d.label, "href": d.href, "note": d.note, "keys": d.keys,
                        "needs": d.needs, "views": list(dict.fromkeys(views))})
        return out

    def search(self, query: str, desk: Optional[str] = None, limit: int = SCREENS) -> list[MapScreen]:
        q = tokens(query)
        ranked = sorted(
            ((score, i) for i, score in enumerate(self._screens.scores(q))
             if score > 0 and (desk is None or self.map.screens[i].desk == desk)),
            key=lambda x: (-x[0], x[1]),
        )
        return [self.map.screens[i] for _, i in ranked[:limit]]

    def search_unseen(self, query: str, desk: Optional[str] = None, limit: int = UNSEEN) -> list[dict[str, Any]]:
        q = tokens(query)
        ranked = sorted(
            ((score, i) for i, score in enumerate(self._unseen_index.scores(q))
             if score > 0 and (desk is None or self._unseen[i][1] == desk)),
            key=lambda x: (-x[0], x[1]),
        )
        return [{"desk": self._unseen[i][1], "file": self._unseen[i][2], "copy": self._unseen[i][0]}
                for _, i in ranked[:limit]]

    def find(self, query: str, desk: Optional[str] = None, requested_build: Optional[str] = None) -> dict[str, Any]:
        """Where things are in the app for `query`: the outline, the best screens and the best unseen copy."""
        labels = {d.id: d.label for d in self.map.desks}
        return {
            "build": self.build,
            "crawled_at": self.crawled_at.isoformat(),
            "matches_build": requested_build is not None and requested_build == self.build,
            "desks": self.outline(),
            "screens": [
                {"id": s.id, "desk": s.desk, "steps": _steps(labels.get(s.desk, s.desk), s), "link": s.link,
                 "signed_in": s.signed_in, "tree": _cut(s.tree)}
                for s in self.search(query, desk)
            ],
            "not_in_demo": self.search_unseen(query, desk),
        }


# ---- Storing ----

def stats_of(upload: AppMapUpload, size: int) -> AppMapStats:
    return AppMapStats(
        screens=len(upload.screens),
        signed_in_screens=sum(1 for s in upload.screens if s.signed_in),
        desks=len(upload.desks),
        unseen_copy=sum(len(c.unseen) for c in upload.copy_),
        findings=len(upload.findings),
        bytes=size,
    )


def _prune() -> int:
    newest = AiAppMap.select(AiAppMap.build).order_by(AiAppMap.crawled_at.desc()).limit(1)
    cutoff = datetime.now(timezone.utc) - RETENTION
    return (AiAppMap
            .delete()
            .where(AiAppMap.crawled_at < cutoff, AiAppMap.build.not_in(newest))
            .execute())


@db_operation("ai.store_app_map")
def _store(build: str, crawled_at: datetime, raw: dict[str, Any], stats: dict[str, Any]) -> tuple[Optional[str], int]:
    with db.atomic():
        # Uploads of one build are serialized here, so the check below holds until the write
        db.execute_sql("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"ai_app_maps:{build}",))
        held = AiAppMap.select(AiAppMap.crawled_at, AiAppMap.stats).where(AiAppMap.build == build).first()
        if held and held.crawled_at > crawled_at:
            # Two crawls of a build can finish out of order (a re-run and the next schedule)
            kept = "Kept the newer map of this build"
        elif held and held.stats.get("signed_in_screens", 0) > 0 and not stats["signed_in_screens"]:
            kept = "Kept the signed-in map of this build"
        else:
            kept = None
            now = datetime.now(timezone.utc)
            (AiAppMap
             .insert(build=build, crawled_at=crawled_at, map=raw, stats=stats, uploaded_at=now)
             .on_conflict(conflict_target=[AiAppMap.build],
                          update={AiAppMap.crawled_at: crawled_at, AiAppMap.map: raw,
                                  AiAppMap.stats: stats, AiAppMap.uploaded_at: now})
             .execute())
        pruned = _prune()
    return kept, pruned


async def store(upload: AppMapUpload, raw: dict[str, Any], size: int) -> tuple[Optional[str], AppMapStats, int]:
    """Store one build's map as uploaded; returns why the held map was kept instead (None when
    this one was stored), its stats and how many maps were pruned."""
    stats = stats_of(upload, size)
    kept, pruned = await _store(upload.build, upload.crawled_at, raw, stats.model_dump())
    if kept:
        log.info("app_map_kept", build=upload.build, reason=kept, pruned=pruned, **stats.model_dump())
    else:
        log.info("app_map_stored", build=upload.build, pruned=pruned, **stats.model_dump())
    return kept, stats, pruned


# ---- Loading ----

_cache: OrderedDict[str, LoadedMap] = OrderedDict()


@db_operation("ai.app_map_build")
def _which(build: Optional[str]) -> Optional[tuple[str, datetime]]:
    """`build` when its map is stored, else the newest build; with when its map was uploaded."""
    if build:
        row = AiAppMap.select(AiAppMap.build, AiAppMap.uploaded_at).where(AiAppMap.build == build).first()
        if row:
            return row.build, row.uploaded_at
    row = AiAppMap.select(AiAppMap.build, AiAppMap.uploaded_at).order_by(AiAppMap.crawled_at.desc()).first()
    return (row.build, row.uploaded_at) if row else None


@db_operation("ai.app_map_load")
def _fetch(build: str) -> Optional[tuple[datetime, datetime, dict[str, Any]]]:
    row = (AiAppMap.select(AiAppMap.crawled_at, AiAppMap.uploaded_at, AiAppMap.map)
           .where(AiAppMap.build == build).first())
    return (row.crawled_at, row.uploaded_at, row.map) if row else None


async def load(build: Optional[str]) -> Optional[LoadedMap]:
    """The map of `build`, or the newest when that build has none yet; None before the first upload.

    Each call asks which upload is current (one small read), so a later crawl
    of a build reaches every worker; only the map itself is cached."""
    found = await _which(build)
    if found is None:
        return None
    found_build, uploaded_at = found
    cached = _cache.get(found_build)
    if cached and cached.uploaded_at == uploaded_at:
        _cache.move_to_end(found_build)
        return cached
    row = await _fetch(found_build)
    if row is None:  # pruned between the two reads
        return None
    crawled_at, uploaded_at, raw = row
    loaded = LoadedMap(build=found_build, crawled_at=crawled_at, map=AppMapUpload.model_validate(raw),
                       uploaded_at=uploaded_at)
    _cache[found_build] = loaded
    _cache.move_to_end(found_build)
    while len(_cache) > CACHED_BUILDS:
        _cache.popitem(last=False)
    return loaded
