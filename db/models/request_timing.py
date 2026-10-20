"""
One API request's timing (migrations/0037__request_timings.sql).

Written in batches by services/request_timings.py; read by data-platform's
Latency page. `parts` splits `duration_ms` into buckets (core/request_timing.py
lists them) and sums to it; `calls` counts each bucket's calls.
"""

from peewee import BigAutoField, CharField, DateTimeField, FloatField, SmallIntegerField, TextField
from playhouse.postgres_ext import BinaryJSONField

from db.base import BaseModel


class RequestTiming(BaseModel):
    id = BigAutoField()
    started_at = DateTimeField()
    service = TextField()
    method = TextField()
    route = TextField()
    status_code = SmallIntegerField()
    duration_ms = FloatField()
    parts = BinaryJSONField()  # {bucket: ms}
    calls = BinaryJSONField()  # {bucket: count}
    trace_id = CharField(max_length=32, null=True)  # set when Railway has the trace
    version = TextField(null=True)

    class Meta:
        table_name = "request_timings"
        schema = "ops"
