from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .config import load_settings


_security = HTTPBasic()


async def require_basic_auth(credentials: Annotated[HTTPBasicCredentials, Depends(_security)]) -> HTTPBasicCredentials:
    settings = load_settings()

    valid_user = secrets.compare_digest(credentials.username or "", settings.basic_auth_user)
    valid_password = secrets.compare_digest(credentials.password or "", settings.basic_auth_password)

    if not (valid_user and valid_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    return credentials
