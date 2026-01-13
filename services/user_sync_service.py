"""
User Sync Service

Handles synchronization between Clerk users and local database users.
Creates local user records on first API call if they don't exist.
"""

from datetime import datetime
from typing import Optional
from db.models import User


class UserSyncService:
    """
    Service to handle syncing Clerk users with the local database.

    When a user authenticates via Clerk and makes an API call, this service
    ensures they have a corresponding record in the local database.
    """

    @staticmethod
    def get_or_create_user(clerk_user_id: str, email: str) -> User:
        """
        Get an existing user by Clerk ID or create a new one.

        This is the primary method to call when handling authenticated requests.
        It ensures the user exists in the local database and returns their record.

        Args:
            clerk_user_id: The user's Clerk ID (from the 'sub' claim in the JWT)
            email: The user's email address

        Returns:
            User: The local database user record
        """
        # Try to find by Clerk ID first (most common case)
        user = User.select().where(User.clerk_user_id == clerk_user_id).first()

        if user:
            # Update email if it changed in Clerk
            if user.email != email:
                user.email = email
                user.save()
            return user

        # Try to find by email (for users who existed before Clerk migration)
        user = User.select().where(User.email == email).first()

        if user:
            # Link existing user to their Clerk account
            user.clerk_user_id = clerk_user_id
            user.save()
            return user

        # Create new user
        user = User.create(
            clerk_user_id=clerk_user_id,
            email=email,
            password=None,  # Clerk handles authentication, no local password needed
            created_at=datetime.now()
        )

        return user

    @staticmethod
    def get_user_by_clerk_id(clerk_user_id: str) -> Optional[User]:
        """
        Get a user by their Clerk ID.

        Args:
            clerk_user_id: The user's Clerk ID

        Returns:
            User or None if not found
        """
        return User.select().where(User.clerk_user_id == clerk_user_id).first()

    @staticmethod
    def get_user_by_email(email: str) -> Optional[User]:
        """
        Get a user by their email address.

        Args:
            email: The user's email address

        Returns:
            User or None if not found
        """
        return User.select().where(User.email == email).first()

    @staticmethod
    def update_user_email(clerk_user_id: str, new_email: str) -> Optional[User]:
        """
        Update a user's email address.

        Args:
            clerk_user_id: The user's Clerk ID
            new_email: The new email address

        Returns:
            Updated User or None if user not found
        """
        user = User.select().where(User.clerk_user_id == clerk_user_id).first()

        if user:
            user.email = new_email
            user.save()

        return user
