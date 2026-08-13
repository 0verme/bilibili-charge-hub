from typing import NoReturn

from fastapi import HTTPException


def error_detail(code: str, message: str) -> dict[str, str]:
    """Return the stable error envelope used by API responses."""
    return {"code": code, "message": message}


def raise_api_error(
    status_code: int,
    code: str,
    message: str,
    *,
    headers: dict[str, str] | None = None,
) -> NoReturn:
    raise HTTPException(
        status_code=status_code,
        detail=error_detail(code, message),
        headers=headers,
    )
