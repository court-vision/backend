"""
Which app map the eval reads: a file the App Map workflow made (its artifact,
`app-map.json`), or a build's map stored in the database in `.env`, the newest
when no build is named. Nothing is written to the database.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


def from_file(path: Path):
    from schemas.app_map import AppMapUpload
    from services.ai.app_map import LoadedMap

    upload = AppMapUpload.model_validate(json.loads(path.read_text()))
    return LoadedMap(build=upload.build, crawled_at=upload.crawled_at, map=upload)


async def from_database(build: Optional[str]):
    from services.ai import app_map

    loaded = await app_map.load(build)
    if loaded is None:
        raise SystemExit("No app map is stored in this database yet")
    if build and loaded.build != build:
        raise SystemExit(f"No map of {build} is stored; the newest is {loaded.build}")
    return loaded


async def load(path: Optional[Path], build: Optional[str]):
    return from_file(path) if path else await from_database(build)
