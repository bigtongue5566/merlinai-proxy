import datetime
import http.client
import json
import os
import threading
import urllib.parse
import uuid
from typing import Any, Dict, List, Optional, Union

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

load_dotenv()

app = FastAPI(title="Merlin API Proxy")

MERLIN_API_URL = "www.getmerlin.in"
MERLIN_PATH = "/arcane/api/v2/thread/unified"
FIREBASE_AUTH_HOST = "identitytoolkit.googleapis.com"
FIREBASE_AUTH_PATH = "/v1/accounts:signInWithPassword"
FIREBASE_REFRESH_HOST = "securetoken.googleapis.com"
FIREBASE_REFRESH_PATH = "/v1/token"
FIREBASE_API_KEY = os.getenv("MERLIN_FIREBASE_API_KEY", "AIzaSyAvCgtQ4XbmlQGIynDT-v_M8eLaXrKmtiM")
MERLIN_EMAIL = os.getenv("MERLIN_EMAIL")
MERLIN_PASSWORD = os.getenv("MERLIN_PASSWORD")
MERLIN_VERSION = os.getenv("MERLIN_VERSION", "iframe-merlin-7.5.19")
PROXY_API_KEY = os.getenv("PROXY_API_KEY", "sk-123")
TOKEN_REFRESH_BUFFER_SECONDS = 60


class ContentPart(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: Optional[str] = None
    text: Optional[str] = None
    input_text: Optional[str] = None
    content: Optional[str] = None


class Message(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: str
    content: Optional[Union[str, List[Union[ContentPart, Dict[str, Any], str]]]] = None


class OpenAIRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str
    messages: List[Message]
    stream: Optional[bool] = False
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None


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


def verify_proxy_api_key(authorization: Optional[str]) -> None:
    expected_header = f"Bearer {PROXY_API_KEY}"
    if authorization != expected_header:
        raise HTTPException(status_code=401, detail="Invalid or missing proxy API key")


def extract_message_text(content: Any) -> str:
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue

            if isinstance(item, BaseModel):
                item = item.model_dump(exclude_none=True)

            if isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("input_text"), str):
                    parts.append(item["input_text"])
                elif item.get("type") == "text" and isinstance(item.get("content"), str):
                    parts.append(item["content"])
        return "".join(parts)

    return ""


def get_last_user_message(messages: List[Message]) -> str:
    for message in reversed(messages):
        if message.role == "user":
            text = extract_message_text(message.content)
            if text:
                return text
    raise HTTPException(status_code=400, detail="No user message content found")


def extract_tool_calls(inner_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_tool_calls = inner_data.get("toolCalls") or inner_data.get("tool_calls") or []
    if not isinstance(raw_tool_calls, list):
        return []

    normalized: List[Dict[str, Any]] = []
    for call in raw_tool_calls:
        if not isinstance(call, dict):
            continue

        function_payload = call.get("function")
        if not isinstance(function_payload, dict):
            function_payload = {
                "name": call.get("name", ""),
                "arguments": call.get("arguments", "{}"),
            }

        function_name = function_payload.get("name")
        function_arguments = function_payload.get("arguments")

        if not isinstance(function_name, str) or not function_name:
            continue

        if isinstance(function_arguments, dict):
            function_arguments = json.dumps(function_arguments)
        elif function_arguments is None:
            function_arguments = "{}"
        elif not isinstance(function_arguments, str):
            function_arguments = str(function_arguments)

        normalized.append(
            {
                "id": call.get("id") or f"call_{uuid.uuid4().hex}",
                "type": "function",
                "function": {
                    "name": function_name,
                    "arguments": function_arguments,
                },
            }
        )

    return normalized


def get_headers() -> Dict[str, str]:
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    timestamp = now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+08:00[Asia/Taipei]"
    return {
        "accept": "text/event-stream",
        "authorization": f"Bearer {token_manager.get_access_token()}",
        "content-type": "application/json",
        "origin": "https://extension.getmerlin.in",
        "referer": "https://extension.getmerlin.in/",
        "user-agent": "Mozilla/5.0",
        "x-merlin-version": MERLIN_VERSION,
        "x-request-timestamp": timestamp,
    }


def merlin_stream_generator(merlin_payload: Dict[str, Any]):
    conn = http.client.HTTPSConnection(MERLIN_API_URL)
    headers = get_headers()

    payload_str = json.dumps(merlin_payload)
    conn.request("POST", MERLIN_PATH, payload_str, headers)
    res = conn.getresponse()

    if res.status != 200:
        error_msg = res.read().decode("utf-8", errors="ignore")
        yield f"data: {json.dumps({'error': {'message': f'Merlin Error: {res.status}', 'details': error_msg}})}\n\n"
        yield "data: [DONE]\n\n"
        conn.close()
        return

    while True:
        line = res.readline()
        if not line:
            break

        line_str = line.decode("utf-8", errors="ignore").strip()
        if line_str.startswith("data:"):
            data_str = line_str[5:].strip()
            if not data_str:
                continue

            if data_str == "[DONE]":
                yield "data: [DONE]\n\n"
                break

            try:
                m_data = json.loads(data_str)
                inner_data = m_data.get("data", {})
                text = inner_data.get("text", "")
                reasoning = inner_data.get("reasoning", "")
                content = inner_data.get("content", "")
                delta_content = text or reasoning or content
                tool_calls = extract_tool_calls(inner_data)

                if delta_content or tool_calls:
                    delta_payload: Dict[str, Any] = {}
                    if delta_content:
                        delta_payload["content"] = delta_content
                    if tool_calls:
                        delta_payload["tool_calls"] = tool_calls

                    openai_chunk = {
                        "id": f"chatcmpl-{uuid.uuid4()}",
                        "object": "chat.completion.chunk",
                        "created": int(datetime.datetime.now().timestamp()),
                        "model": merlin_payload["model"],
                        "choices": [{"index": 0, "delta": delta_payload, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(openai_chunk)}\n\n"
            except json.JSONDecodeError:
                continue
    conn.close()


@app.post("/v1/chat/completions")
async def chat_completions(request: OpenAIRequest, authorization: Optional[str] = Header(default=None)):
    verify_proxy_api_key(authorization)
    user_msg = get_last_user_message(request.messages)

    merlin_payload = {
        "attachments": [],
        "chatId": str(uuid.uuid4()),
        "language": "AUTO",
        "message": {
            "childId": str(uuid.uuid4()),
            "content": user_msg,
            "context": "",
            "id": str(uuid.uuid4()),
            "parentId": "root",
        },
        "mode": "UNIFIED_CHAT",
        "model": request.model,
        "metadata": {
            "deepResearch": False,
            "merlinMagic": False,
            "noTask": True,
            "proFinderMode": False,
            "mcpConfig": {
                "isEnabled": bool(request.tools),
                "tools": request.tools or [],
                "toolChoice": request.tool_choice,
            },
            "isWebpageChat": False,
            "webAccess": False,
        },
    }

    if request.stream:
        return StreamingResponse(merlin_stream_generator(merlin_payload), media_type="text/event-stream")

    conn = http.client.HTTPSConnection(MERLIN_API_URL)
    headers = get_headers()
    conn.request("POST", MERLIN_PATH, json.dumps(merlin_payload), headers)
    res = conn.getresponse()

    if res.status != 200:
        raise HTTPException(status_code=res.status, detail=res.read().decode())

    full_content = ""
    response_tool_calls: List[Dict[str, Any]] = []
    while True:
        line = res.readline()
        if not line:
            break
        line_str = line.decode("utf-8", errors="ignore").strip()
        if line_str.startswith("data:"):
            data_str = line_str[5:].strip()
            if data_str == "[DONE]":
                break
            if not data_str:
                continue
            try:
                m_data = json.loads(data_str)
                inner_data = m_data.get("data", {})
                text = inner_data.get("text", "")
                reasoning = inner_data.get("reasoning", "")
                content = inner_data.get("content", "")
                full_content += text or reasoning or content
                response_tool_calls.extend(extract_tool_calls(inner_data))
            except json.JSONDecodeError:
                continue
    conn.close()

    response_message: Dict[str, Any] = {"role": "assistant", "content": full_content or None}
    finish_reason = "stop"
    if response_tool_calls:
        response_message["tool_calls"] = response_tool_calls
        finish_reason = "tool_calls"

    return {
        "id": f"chatcmpl-{uuid.uuid4()}",
        "object": "chat.completion",
        "created": int(datetime.datetime.now().timestamp()),
        "model": request.model,
        "choices": [{"index": 0, "message": response_message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@app.get("/v1/models")
async def list_models(authorization: Optional[str] = Header(default=None)):
    verify_proxy_api_key(authorization)
    models = [
        "gpt-5.4",
        "gemini-3.1-flash-lite",
        "gemini-3.1-pro",
        "claude-4.6-sonnet",
        "claude-4.6-opus",
        "glm-5",
        "minimax-m2.5",
    ]
    return {
        "object": "list",
        "data": [
            {
                "id": model,
                "object": "model",
                "created": int(datetime.datetime.now().timestamp()),
                "owned_by": "merlin",
            }
            for model in models
        ],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
