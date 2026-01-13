"""
User Sync Service

Handles synchronization between Clerk users and local database users.
Creates local user records on first API call if they don't exist.
"""

from datetime import datetime
from typing import Optional
from db.models import User
from fastapi import HTTPException


class UserSyncService:
    """
    Service to handle syncing Clerk users with the local database.

    When a user authenticates via Clerk and makes an API call, this service
    ensures they have a corresponding record in the local database.
    """

    # In-memory cache to avoid repeated Clerk API calls during a request
    _user_cache: dict = {}

    @staticmethod
    def get_or_create_user(clerk_user_id: str, email: Optional[str] = None) -> User:
        """
        Get an existing user by Clerk ID or create a new one.

        This is the primary method to call when handling authenticated requests.
        It ensures the user exists in the local database and returns their record.

        Args:
            clerk_user_id: The user's Clerk ID (from the 'sub' claim in the JWT)
            email: The user's email address (optional, fetched from Clerk API if needed)

        Returns:
            User: The local database user record

        Raises:
            HTTPException: If user cannot be created (e.g., no email available)
        """
        # Try to find by Clerk ID first (most common case)
        user = User.select().where(User.clerk_user_id == clerk_user_id).first()

        if user:
            # Update email if provided and different
            if email and user.email != email:
                user.email = email
                user.save()
            return user

        # If we have an email, try to find by email (for users who existed before Clerk migration)
        if email:
            user = User.select().where(User.email == email).first()

            if user:
                # Link existing user to their Clerk account
                user.clerk_user_id = clerk_user_id
                user.save()
                return user

        # Need to create a new user - email is required
        if not email:
            # This shouldn't happen if CLERK_SECRET_KEY is configured properly
            print(f"Error: Cannot create user {clerk_user_id} - no email available. "
                  "Ensure CLERK_SECRET_KEY is set in backend environment.")
            raise HTTPException(
                status_code=500,
                detail="Unable to fetch user email. Please try again or contact support."
            )

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
