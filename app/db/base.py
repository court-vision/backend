from playhouse.pool import PooledPostgresqlDatabase
from peewee import Model

db = PooledPostgresqlDatabase(
    'railway',
    user='postgres',
    password='rtVSWuhkROFxOpegJuElubldctbpXmBT',
    host='caboose.proxy.rlwy.net',
    port=33942,
    max_connections=20,
    stale_timeout=300,
    timeout=10,
)

class BaseModel(Model):
    class Meta:
        database = db