from fastapi import APIRouter, Depends
from services.user_service import UserService
from services.user_sync_service import UserSyncService
from schemas.user import UserUpdateReq, UserDeleteReq, UserUpdateResp, UserDeleteResp
from core.clerk_auth import get_current_user

router = APIRouter(prefix="/users", tags=["user management"])


def _get_user_id(current_user: dict) -> int:
    """Helper to get local user_id from Clerk user info."""
    clerk_user_id = current_user.get("clerk_user_id")
    email = current_user.get("email")
    user = UserSyncService.get_or_create_user(clerk_user_id, email)
    return user.user_id


@router.post('/update', response_model=UserUpdateResp)
async def update_user(user_info: UserUpdateReq, current_user: dict = Depends(get_current_user)):
    user_id = _get_user_id(current_user)
    # Note: With Clerk, email/password changes should be done through Clerk's UI
    # This endpoint now only updates local user data if needed
    return await UserService.update_user(user_id, user_info.email, user_info.password)

@router.post('/delete', response_model=UserDeleteResp)
async def delete_user(user: UserDeleteReq, current_user: dict = Depends(get_current_user)):
    user_id = _get_user_id(current_user)
    # Note: With Clerk, the user is already authenticated via their token
    # Password verification is no longer needed - Clerk handles authentication
    # To fully delete, you should also delete the user from Clerk via their API
    return await UserService.delete_user(user_id, user.password)
