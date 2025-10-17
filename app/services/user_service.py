from typing import Optional
from app.schemas.user import UserUpdateResp, UserDeleteResp
from app.schemas.common import ApiStatus
from app.core.security import check_password, hash_password
from app.db.models import User

class UserService:
    
    @staticmethod
    async def update_user(user_id: int, email: Optional[str], password: Optional[str]) -> UserUpdateResp:
        try:
            update_data = {}
            if email:
                update_data['email'] = email
            if password:
                update_data['password'] = hash_password(password)
            
            if update_data:
                User.update(**update_data).where(User.user_id == user_id).execute()
        
            return UserUpdateResp(success=True)
            
        except Exception as e:
            print(f"Error in update_user: {e}")
            return UserUpdateResp(success=False)

    @staticmethod
    async def delete_user(user_id: int, password: str) -> UserDeleteResp:
        try:
            user_data = User.select().where(User.user_id == user_id).first()

            if not user_data or not check_password(password, user_data.password):
                return UserDeleteResp(success=False)

            # Delete user (teams will be deleted automatically due to CASCADE)
            User.delete().where(User.user_id == user_id).execute()

            return UserDeleteResp(success=True)
            
        except Exception as e:
            print(f"Error in delete_user: {e}")
            return UserDeleteResp(success=False)
