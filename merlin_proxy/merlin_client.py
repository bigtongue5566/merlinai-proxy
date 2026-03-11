import datetime
import http.client
import json
import uuid
from typing import Any, Dict, Iterator, List

from fastapi import HTTPException

from .auth import token_manager
from .config import MERLIN_API_URL, MERLIN_PATH, MERLIN_VERSION
from .logging_config import log_debug_payload
from .openai_adapter import build_streamed_openai_response, extract_tool_calls
from .schemas import OpenAIRequest


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
                "tools": request.tools or [],
                "toolChoice": request.tool_choice,
            },
            "isWebpageChat": False,
            "webAccess": False,
        },
    }


def _read_merlin_event_stream(res: http.client.HTTPResponse) -> tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
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
            merlin_data = json.loads(data_str)
            raw_events.append(merlin_data)
            inner_data = merlin_data.get("data", {})
            text = inner_data.get("text", "")
            reasoning = inner_data.get("reasoning", "")
            content = inner_data.get("content", "")
            full_content += text or reasoning or content
            response_tool_calls.extend(extract_tool_calls(inner_data))
        except json.JSONDecodeError:
            continue

    return full_content, response_tool_calls, raw_events


def merlin_stream_generator(merlin_payload: Dict[str, Any], request: OpenAIRequest) -> Iterator[str]:
    conn = http.client.HTTPSConnection(MERLIN_API_URL)
    try:
        conn.request("POST", MERLIN_PATH, json.dumps(merlin_payload), get_headers())
        res = conn.getresponse()

        if res.status != 200:
            error_msg = res.read().decode("utf-8", errors="ignore")
            log_debug_payload("merlin_stream_error", {"status": res.status, "body": error_msg})
            yield f"data: {json.dumps({'error': {'message': f'Merlin Error: {res.status}', 'details': error_msg}})}\n\n"
            yield "data: [DONE]\n\n"
            return

        full_content, response_tool_calls, raw_events = _read_merlin_event_stream(res)
        log_debug_payload(
            "merlin_stream_summary",
            {
                "merlin_event_count": len(raw_events),
                "merlin_event_sample": raw_events[:3],
                "assembled_content": full_content,
                "tool_call_count": len(response_tool_calls),
            },
        )
        yield from build_streamed_openai_response(request, full_content, response_tool_calls)
    finally:
        conn.close()


def send_merlin_request(merlin_payload: Dict[str, Any]) -> tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    conn = http.client.HTTPSConnection(MERLIN_API_URL)
    try:
        conn.request("POST", MERLIN_PATH, json.dumps(merlin_payload), get_headers())
        res = conn.getresponse()

        if res.status != 200:
            error_body = res.read().decode("utf-8", errors="ignore")
            log_debug_payload("merlin_non_stream_error", {"status": res.status, "body": error_body})
            raise HTTPException(status_code=res.status, detail=error_body)

        return _read_merlin_event_stream(res)
    finally:
        conn.close()
