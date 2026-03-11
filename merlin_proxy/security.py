from typing import Optional

from fastapi import HTTPException

from .config import PROXY_API_KEY


def verify_proxy_api_key(authorization: Optional[str]) -> None:
    expected_header = f"Bearer {PROXY_API_KEY}"
    if authorization != expected_header:
        raise HTTPException(status_code=401, detail="Invalid or missing proxy API key")
