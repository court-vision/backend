from fastapi import APIRouter, Depends
from app.services.user_service import UserService
from app.schemas.user import UserUpdateReq, UserDeleteReq, UserUpdateResp, UserDeleteResp
from app.core.security import get_current_user

router = APIRouter(prefix="/users", tags=["user management"])

@router.post('/update', response_model=UserUpdateResp)
async def update_user(user_info: UserUpdateReq, current_user: dict = Depends(get_current_user)):
    user_id = current_user.get("uid")
    return await UserService.update_user(user_id, user_info.email, user_info.password)

@router.post('/delete', response_model=UserDeleteResp)
async def delete_user(user: UserDeleteReq, current_user: dict = Depends(get_current_user)):
    user_id = current_user.get("uid")
    return await UserService.delete_user(user_id, user.password)
