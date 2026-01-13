from peewee import (
    AutoField,
    CharField,
    DateTimeField,
)
from db.base import BaseModel


class User(BaseModel):
    user_id = AutoField(primary_key=True)
    clerk_user_id = CharField(max_length=255, unique=True, null=True)  # Clerk user ID
    email = CharField(max_length=255, unique=True)
    password = CharField(max_length=255, null=True)  # hashed password (nullable for Clerk users)
    created_at = DateTimeField(default=None, null=True)

    class Meta:
        table_name = "users"
        schema = "usr"

    def __repr__(self):
        return f"<User(user_id={self.user_id}, clerk_user_id='{self.clerk_user_id}', email='{self.email}')>"
