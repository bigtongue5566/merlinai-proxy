import datetime
import http.client
import json
import uuid
from typing import Any, Dict, List

from fastapi import HTTPException

from .auth import token_manager
from .config import MERLIN_API_URL, MERLIN_PATH, MERLIN_VERSION
from .logging_config import log_debug_payload
from .schemas import OpenAIRequest
from .tool_payload_parser import extract_tool_calls
from .tool_prompt import compact_tools_for_prompt, get_allowed_tool_names


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


def build_merlin_payload(request: OpenAIRequest, user_message: str) -> Dict[str, Any]:
    compact_tools = compact_tools_for_prompt(request.tools)
    return {
        "attachments": [],
        "chatId": str(uuid.uuid4()),
        "language": "AUTO",
        "message": {
            "childId": str(uuid.uuid4()),
            "content": user_message,
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
                "tools": compact_tools,
                "toolChoice": request.tool_choice,
            },
            "isWebpageChat": False,
            "webAccess": False,
        },
    }


def send_merlin_request(
    merlin_payload: Dict[str, Any], request: OpenAIRequest
) -> tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    conn = http.client.HTTPSConnection(MERLIN_API_URL)
    try:
        conn.request("POST", MERLIN_PATH, json.dumps(merlin_payload), get_headers())
        res = conn.getresponse()

        if res.status != 200:
            error_body = res.read().decode("utf-8", errors="ignore")
            log_debug_payload("merlin_non_stream_error", {"status": res.status, "body": error_body})
            raise HTTPException(status_code=res.status, detail=error_body)

        return _read_merlin_event_stream_with_allowed_tools(res, request)
    finally:
        conn.close()


def _read_merlin_event_stream_with_allowed_tools(
    res: http.client.HTTPResponse, request: OpenAIRequest
) -> tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    full_content = ""
    response_tool_calls: List[Dict[str, Any]] = []
    raw_events: List[Dict[str, Any]] = []
    allowed_tool_names = get_allowed_tool_names(request)

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
            merlin_data = json.loads(data_str)
            raw_events.append(merlin_data)
            inner_data = merlin_data.get("data", {})
            text = inner_data.get("text", "")
            content = inner_data.get("content", "")
            full_content += text or content
            response_tool_calls.extend(extract_tool_calls(inner_data, allowed_tool_names))
        except json.JSONDecodeError:
            continue

    return full_content, response_tool_calls, raw_events
