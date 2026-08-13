from pydantic import BaseModel, Field, field_validator

from app.models import UserRole


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=12, max_length=128)

    @field_validator("password")
    @classmethod
    def require_password_variety(cls, value: str) -> str:
        if not any(char.isalpha() for char in value) or not any(char.isdigit() for char in value):
            raise ValueError("password must contain letters and numbers")
        return value


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
    new_password: str = Field(min_length=12, max_length=128)

    _validate_new = field_validator("new_password")(Credentials.require_password_variety.__func__)


class PasswordReset(BaseModel):
    new_password: str = Field(min_length=12, max_length=128)

    _validate_new = field_validator("new_password")(Credentials.require_password_variety.__func__)


class UserUpdate(BaseModel):
    is_active: bool
