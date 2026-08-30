from pydantic import BaseModel, Field

from app.models import UserRole


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=1, max_length=128)


class UserCreate(Credentials):
    role: UserRole = UserRole.USER


class UserView(BaseModel):
    id: str
    username: str
    role: UserRole
    is_active: bool

    model_config = {"from_attributes": True}


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=128)


class PasswordReset(BaseModel):
    new_password: str = Field(min_length=1, max_length=128)


class AdminRecovery(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    recovery_token: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=128)


class UserUpdate(BaseModel):
    is_active: bool
