from peewee import (
    Model,
    CharField,
    IntegerField,
)
from db.base import BaseModel


class Verification(BaseModel):
    email = CharField(max_length=255)
    code = CharField(max_length=6)
    hashed_password = CharField(max_length=255)
    timestamp = IntegerField()
    type = CharField(max_length=50, default="email")

    class Meta:
        table_name = "verifications"
        schema = "usr"

    def __repr__(self):
        return f"<Verification(email='{self.email}', type='{self.type}')>"
