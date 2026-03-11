import datetime
import http.client
import json
import threading
import urllib.parse
from typing import Any, Dict, Optional

from fastapi import HTTPException

from .config import (
    FIREBASE_API_KEY,
    FIREBASE_AUTH_HOST,
    FIREBASE_AUTH_PATH,
    FIREBASE_REFRESH_HOST,
    FIREBASE_REFRESH_PATH,
    MERLIN_EMAIL,
    MERLIN_PASSWORD,
    TOKEN_REFRESH_BUFFER_SECONDS,
)


class MerlinTokenManager:
    def __init__(self) -> None:
        self._id_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._expires_at: Optional[datetime.datetime] = None
        self._lock = threading.Lock()

    def get_access_token(self) -> str:
        with self._lock:
            if self._has_valid_token():
                return self._id_token  # type: ignore[return-value]

            if self._refresh_token:
                try:
                    self._refresh_access_token()
                    return self._id_token  # type: ignore[return-value]
                except HTTPException:
                    self._clear_tokens()

            self._sign_in()
            return self._id_token  # type: ignore[return-value]

    def _has_valid_token(self) -> bool:
        if not self._id_token or not self._expires_at:
            return False
        return datetime.datetime.now(datetime.timezone.utc) < self._expires_at

    def _set_tokens(self, *, id_token: str, refresh_token: str, expires_in: str) -> None:
        lifetime_seconds = max(int(expires_in) - TOKEN_REFRESH_BUFFER_SECONDS, 0)
        self._id_token = id_token
        self._refresh_token = refresh_token
        self._expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=lifetime_seconds)

    def _clear_tokens(self) -> None:
        self._id_token = None
        self._refresh_token = None
        self._expires_at = None

    def _sign_in(self) -> None:
        if not MERLIN_EMAIL or not MERLIN_PASSWORD:
            raise HTTPException(status_code=500, detail="Missing MERLIN_EMAIL or MERLIN_PASSWORD environment variables")

        payload = json.dumps(
            {
                "email": MERLIN_EMAIL,
                "password": MERLIN_PASSWORD,
                "returnSecureToken": True,
            }
        )
        path = f"{FIREBASE_AUTH_PATH}?key={FIREBASE_API_KEY}"
        headers = {"content-type": "application/json"}
        data = self._request_json(FIREBASE_AUTH_HOST, path, payload, headers)
        self._set_tokens(
            id_token=data["idToken"],
            refresh_token=data["refreshToken"],
            expires_in=data["expiresIn"],
        )

    def _refresh_access_token(self) -> None:
        payload = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
            }
        )
        path = f"{FIREBASE_REFRESH_PATH}?key={FIREBASE_API_KEY}"
        headers = {"content-type": "application/x-www-form-urlencoded"}
        data = self._request_json(FIREBASE_REFRESH_HOST, path, payload, headers)
        self._set_tokens(
            id_token=data["id_token"],
            refresh_token=data["refresh_token"],
            expires_in=data["expires_in"],
        )

    def _request_json(self, host: str, path: str, payload: str, headers: Dict[str, str]) -> Dict[str, Any]:
        conn = http.client.HTTPSConnection(host)
        try:
            conn.request("POST", path, payload, headers)
            res = conn.getresponse()
            body = res.read().decode("utf-8", errors="ignore")
        finally:
            conn.close()

        if res.status != 200:
            raise HTTPException(status_code=502, detail=f"Firebase auth failed: {body}")

        data = json.loads(body)
        if "error" in data:
            raise HTTPException(status_code=502, detail=f"Firebase auth error: {data['error']}")
        return data


token_manager = MerlinTokenManager()
