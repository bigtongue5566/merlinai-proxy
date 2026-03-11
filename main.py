import datetime
import http.client
import json
import logging
import os
from pathlib import Path
import threading
import urllib.parse
import uuid
from logging.handlers import RotatingFileHandler
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
LOG_LEVEL_NAME = os.getenv("LOG_LEVEL", "INFO").upper()
DEBUG_PROXY_LOG_PATH = Path(os.getenv("DEBUG_PROXY_LOG_PATH", "proxy-debug.log"))
if not DEBUG_PROXY_LOG_PATH.is_absolute():
    DEBUG_PROXY_LOG_PATH = Path(__file__).resolve().parent / DEBUG_PROXY_LOG_PATH
DEBUG_PROXY_LOG_MAX_BYTES = int(os.getenv("DEBUG_PROXY_LOG_MAX_BYTES", "1048576"))
DEBUG_PROXY_LOG_BACKUP_COUNT = int(os.getenv("DEBUG_PROXY_LOG_BACKUP_COUNT", "3"))
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


def build_debug_logger() -> logging.Logger:
    logger = logging.getLogger("merlin_proxy")
    logger.handlers.clear()
    logger.propagate = False
    log_level = getattr(logging, LOG_LEVEL_NAME, logging.INFO)
    logger.setLevel(log_level)

    formatter = logging.Formatter("[proxy] %(asctime)s %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%S%z")

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(log_level)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    DEBUG_PROXY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        DEBUG_PROXY_LOG_PATH,
        maxBytes=DEBUG_PROXY_LOG_MAX_BYTES,
        backupCount=DEBUG_PROXY_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


logger = build_debug_logger()


def debug_log(label: str, payload: Any) -> None:
    if not logger.isEnabledFor(logging.DEBUG):
        return

    try:
        body = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    except TypeError:
        body = str(payload)
    logger.debug("%s:\n%s", label, body)


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


def normalize_tool_choice(tool_choice: Optional[Union[str, Dict[str, Any]]]) -> Optional[str]:
    if isinstance(tool_choice, str):
        return tool_choice

    if isinstance(tool_choice, dict):
        if tool_choice.get("type") == "function":
            function_name = tool_choice.get("function", {}).get("name")
            if isinstance(function_name, str) and function_name:
                return f"function:{function_name}"

        raw_type = tool_choice.get("type")
        if isinstance(raw_type, str) and raw_type:
            return raw_type

    return None


def should_force_tool_json(request: OpenAIRequest) -> bool:
    return bool(request.tools)


def build_conversation_transcript(messages: List[Message]) -> str:
    transcript_parts: List[str] = []
    for message in messages:
        text = extract_message_text(message.content)
        if text:
            transcript_parts.append(f"{message.role.upper()}: {text}")
    return "\n\n".join(transcript_parts)


def build_tool_prompt(request: OpenAIRequest) -> str:
    tool_choice = normalize_tool_choice(request.tool_choice) or "auto"
    tools_json = json.dumps(request.tools or [], ensure_ascii=False, indent=2)
    transcript = build_conversation_transcript(request.messages)

    return (
        "You are acting as an OpenAI-compatible assistant with tool calling.\n"
        "Return exactly one JSON object only, with no markdown, no commentary, and no reasoning text.\n"
        f"tool_choice={tool_choice}\n\n"
        "Conversation transcript:\n"
        f"{transcript}\n\n"
        "Available tools:\n"
        f"{tools_json}\n\n"
        "Valid output formats:\n"
        '{"type":"tool_calls","tool_calls":[{"name":"tool_name","arguments":{}}]}\n'
        '{"type":"message","content":"final answer"}\n'
        "If tool_choice requires a function, you must return tool_calls for that function.\n"
        "Arguments must always be a JSON object.\n"
        "Do not include any text before or after the JSON object.\n"
    )


def extract_last_json_object(raw_text: str) -> Optional[str]:
    in_string = False
    escape = False
    depth = 0
    start_index: Optional[int] = None
    completed_objects: List[str] = []

    for index, char in enumerate(raw_text):
        if escape:
            escape = False
            continue

        if char == "\\" and in_string:
            escape = True
            continue

        if char == '"':
            in_string = not in_string
            continue

        if in_string:
            continue

        if char == "{":
            if depth == 0:
                start_index = index
            depth += 1
            continue

        if char == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start_index is not None:
                completed_objects.append(raw_text[start_index : index + 1])
                start_index = None

    return completed_objects[-1] if completed_objects else None


def try_parse_json_object(raw_text: str) -> Optional[Dict[str, Any]]:
    text = raw_text.strip()
    if not text:
        return None

    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    last_json_object = extract_last_json_object(text)
    if not last_json_object:
        return None

    try:
        parsed = json.loads(last_json_object)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


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
            function_arguments = json.dumps(function_arguments, ensure_ascii=False)
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


def extract_tool_calls_from_json_payload(payload: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return []

    raw_tool_calls = payload.get("tool_calls")
    if not isinstance(raw_tool_calls, list):
        return []

    normalized: List[Dict[str, Any]] = []
    for call in raw_tool_calls:
        if not isinstance(call, dict):
            continue

        name = call.get("name")
        arguments = call.get("arguments", {})
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(arguments, dict):
            arguments = {}

        normalized.append(
            {
                "id": f"call_{uuid.uuid4().hex}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }
        )

    return normalized


def build_openai_response(request: OpenAIRequest, full_content: str, response_tool_calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    parsed_payload = try_parse_json_object(full_content) if should_force_tool_json(request) else None
    json_tool_calls = extract_tool_calls_from_json_payload(parsed_payload)
    all_tool_calls = response_tool_calls or json_tool_calls

    response_message: Dict[str, Any] = {"role": "assistant", "content": full_content or None}
    finish_reason = "stop"

    if parsed_payload and parsed_payload.get("type") == "message" and isinstance(parsed_payload.get("content"), str):
        response_message["content"] = parsed_payload["content"]

    if all_tool_calls:
        response_message["content"] = None
        response_message["tool_calls"] = all_tool_calls
        finish_reason = "tool_calls"

    required_tool_call = normalize_tool_choice(request.tool_choice)
    if finish_reason != "tool_calls" and required_tool_call in {"required"}:
        raise HTTPException(
            status_code=422,
            detail="Tool calling was required, but upstream did not return a valid tool call payload.",
        )
    if finish_reason != "tool_calls" and isinstance(required_tool_call, str) and required_tool_call.startswith("function:"):
        raise HTTPException(
            status_code=422,
            detail=f"Specific tool call was required ({required_tool_call}), but upstream did not return a valid tool call payload.",
        )

    return {
        "id": f"chatcmpl-{uuid.uuid4()}",
        "object": "chat.completion",
        "created": int(datetime.datetime.now().timestamp()),
        "model": request.model,
        "choices": [{"index": 0, "message": response_message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def build_streamed_openai_response(request: OpenAIRequest, full_content: str, response_tool_calls: List[Dict[str, Any]]):
    response_payload = build_openai_response(request, full_content, response_tool_calls)
    response_id = response_payload["id"]
    created = response_payload["created"]
    choice = response_payload["choices"][0]
    finish_reason = choice["finish_reason"]
    message = choice["message"]
    debug_log(
        "streamed_openai_response_summary",
        {
            "response_id": response_id,
            "finish_reason": finish_reason,
            "tool_call_names": [tool_call["function"]["name"] for tool_call in message.get("tool_calls", [])],
            "content_preview": (message.get("content") or "")[:300],
        },
    )

    yield f"data: {json.dumps({'id': response_id, 'object': 'chat.completion.chunk', 'created': created, 'model': request.model, 'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]})}\n\n"

    if finish_reason == "tool_calls":
        for index, tool_call in enumerate(message["tool_calls"]):
            yield f"data: {json.dumps({'id': response_id, 'object': 'chat.completion.chunk', 'created': created, 'model': request.model, 'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': index, 'id': tool_call['id'], 'type': 'function', 'function': {'name': tool_call['function']['name'], 'arguments': tool_call['function']['arguments']}}]}, 'finish_reason': None}]})}\n\n"
        yield f"data: {json.dumps({'id': response_id, 'object': 'chat.completion.chunk', 'created': created, 'model': request.model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]})}\n\n"
        yield "data: [DONE]\n\n"
        return

    content = message.get("content") or ""
    if content:
        yield f"data: {json.dumps({'id': response_id, 'object': 'chat.completion.chunk', 'created': created, 'model': request.model, 'choices': [{'index': 0, 'delta': {'content': content}, 'finish_reason': None}]})}\n\n"

    yield f"data: {json.dumps({'id': response_id, 'object': 'chat.completion.chunk', 'created': created, 'model': request.model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})}\n\n"
    yield "data: [DONE]\n\n"


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


def merlin_stream_generator(merlin_payload: Dict[str, Any], request: OpenAIRequest):
    conn = http.client.HTTPSConnection(MERLIN_API_URL)
    headers = get_headers()

    payload_str = json.dumps(merlin_payload)
    conn.request("POST", MERLIN_PATH, payload_str, headers)
    res = conn.getresponse()

    if res.status != 200:
        error_msg = res.read().decode("utf-8", errors="ignore")
        debug_log("merlin_stream_error", {"status": res.status, "body": error_msg})
        yield f"data: {json.dumps({'error': {'message': f'Merlin Error: {res.status}', 'details': error_msg}})}\n\n"
        yield "data: [DONE]\n\n"
        conn.close()
        return

    full_content = ""
    response_tool_calls: List[Dict[str, Any]] = []
    raw_events: List[Dict[str, Any]] = []
    while True:
        line = res.readline()
        if not line:
            break

        line_str = line.decode("utf-8", errors="ignore").strip()
        if not line_str.startswith("data:"):
            continue

        data_str = line_str[5:].strip()
        if not data_str:
            continue
        if data_str == "[DONE]":
            break

        try:
            m_data = json.loads(data_str)
            raw_events.append(m_data)
            inner_data = m_data.get("data", {})
            text = inner_data.get("text", "")
            reasoning = inner_data.get("reasoning", "")
            content = inner_data.get("content", "")
            full_content += text or reasoning or content
            response_tool_calls.extend(extract_tool_calls(inner_data))
        except json.JSONDecodeError:
            continue
    conn.close()

    debug_log(
        "merlin_stream_summary",
        {
            "merlin_event_count": len(raw_events),
            "merlin_event_sample": raw_events[:3],
            "assembled_content": full_content,
            "tool_call_count": len(response_tool_calls),
        },
    )

    yield from build_streamed_openai_response(request, full_content, response_tool_calls)


@app.post("/v1/chat/completions")
async def chat_completions(request: OpenAIRequest, authorization: Optional[str] = Header(default=None)):
    verify_proxy_api_key(authorization)
    debug_log(
        "incoming_chat_request",
        {
            "model": request.model,
            "stream": request.stream,
            "has_tools": bool(request.tools),
            "tool_choice": request.tool_choice,
            "message_count": len(request.messages),
        },
    )
    user_msg = build_tool_prompt(request) if should_force_tool_json(request) else get_last_user_message(request.messages)

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
    debug_log("outgoing_merlin_payload", merlin_payload)

    if request.stream:
        return StreamingResponse(merlin_stream_generator(merlin_payload, request), media_type="text/event-stream")

    conn = http.client.HTTPSConnection(MERLIN_API_URL)
    headers = get_headers()
    conn.request("POST", MERLIN_PATH, json.dumps(merlin_payload), headers)
    res = conn.getresponse()

    if res.status != 200:
        error_body = res.read().decode()
        debug_log("merlin_non_stream_error", {"status": res.status, "body": error_body})
        raise HTTPException(status_code=res.status, detail=error_body)

    full_content = ""
    response_tool_calls: List[Dict[str, Any]] = []
    raw_events: List[Dict[str, Any]] = []
    while True:
        line = res.readline()
        if not line:
            break
        line_str = line.decode("utf-8", errors="ignore").strip()
        if not line_str.startswith("data:"):
            continue

        data_str = line_str[5:].strip()
        if data_str == "[DONE]":
            break
        if not data_str:
            continue

        try:
            m_data = json.loads(data_str)
            raw_events.append(m_data)
            inner_data = m_data.get("data", {})
            text = inner_data.get("text", "")
            reasoning = inner_data.get("reasoning", "")
            content = inner_data.get("content", "")
            full_content += text or reasoning or content
            response_tool_calls.extend(extract_tool_calls(inner_data))
        except json.JSONDecodeError:
            continue
    conn.close()

    response_payload = build_openai_response(request, full_content, response_tool_calls)
    debug_log(
        "outgoing_openai_response",
        {
            "response": response_payload,
            "merlin_event_count": len(raw_events),
            "merlin_event_sample": raw_events[:3],
        },
    )
    return response_payload


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
