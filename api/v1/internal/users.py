from fastapi import APIRouter, Depends
from services.user_service import UserService
from schemas.user import UserUpdateReq, UserDeleteReq, UserUpdateResp, UserDeleteResp
from api.deps import UserContext, get_db_user

router = APIRouter(prefix="/users", tags=["user management"])


@router.post('/update', response_model=UserUpdateResp)
async def update_user(user_info: UserUpdateReq, user: UserContext = Depends(get_db_user)):
    # Note: With Clerk, email/password changes should be done through Clerk's UI
    # This endpoint now only updates local user data if needed
    return await UserService.update_user(user.user_id, user_info.email, user_info.password)

@router.post('/delete', response_model=UserDeleteResp)
async def delete_user(req: UserDeleteReq, user: UserContext = Depends(get_db_user)):
    # Note: With Clerk, the user is already authenticated via their token
    # Password verification is no longer needed - Clerk handles authentication
    # To fully delete, you should also delete the user from Clerk via their API
    return await UserService.delete_user(user.user_id, req.password)
