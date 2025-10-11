from peewee import (
    Model,
    IntegerField,
    CharField,
    DateTimeField,
)
from app.db.base import BaseModel


class User(BaseModel):
    user_id = IntegerField(primary_key=True)
    email = CharField(max_length=255, unique=True)
    password = CharField(max_length=255)  # hashed password
    created_at = DateTimeField(default=None, null=True)

    class Meta:
        table_name = "users"
        schema = "usr"

    def __repr__(self):
        return f"<User(user_id={self.user_id}, email='{self.email}')>"
