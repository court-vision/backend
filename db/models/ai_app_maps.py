"""
usr.ai_app_maps — the app map of each crawled frontend build (migration 0037,
docs/APP_MAP_PLAN.md § 4): what CV AI reads to answer "where in the app can I…".

Written only by the App Map workflow's upload (`services.ai.app_map.store`),
read by the agent. Backend-only; data-platform never reads or writes it.
"""

from datetime import datetime, timezone

from peewee import DateTimeField, TextField
from playhouse.postgres_ext import BinaryJSONField

from db.base import BaseModel


class AiAppMap(BaseModel):
    build = TextField(primary_key=True)
    crawled_at = DateTimeField()
    map = BinaryJSONField()
    stats = BinaryJSONField(default=dict)
    uploaded_at = DateTimeField(default=lambda: datetime.now(timezone.utc))

    class Meta:
        table_name = "ai_app_maps"
        schema = "usr"
