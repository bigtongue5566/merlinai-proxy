from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Header
from fastapi.responses import StreamingResponse

from .logging_config import configure_logger, log_debug_payload
from .merlin_client import build_merlin_payload, merlin_stream_generator, send_merlin_request
from .openai_adapter import (
    build_openai_response,
    build_tool_prompt,
    get_last_user_message,
    should_force_tool_json,
)
from .schemas import OpenAIRequest
from .security import verify_proxy_api_key

configure_logger()

app = FastAPI(title="Merlin API Proxy")


@app.post("/v1/chat/completions")
async def chat_completions(request: OpenAIRequest, authorization: Optional[str] = Header(default=None)):
    verify_proxy_api_key(authorization)
    log_debug_payload(
        "incoming_chat_request",
        {
            "model": request.model,
            "stream": request.stream,
            "has_tools": bool(request.tools),
            "tool_choice": request.tool_choice,
            "message_count": len(request.messages),
        },
    )

    user_message = build_tool_prompt(request) if should_force_tool_json(request) else get_last_user_message(request.messages)
    merlin_payload = build_merlin_payload(request, user_message)
    log_debug_payload("outgoing_merlin_payload", merlin_payload)

    if request.stream:
        return StreamingResponse(merlin_stream_generator(merlin_payload, request), media_type="text/event-stream")

    full_content, response_tool_calls, raw_events = send_merlin_request(merlin_payload)
    response_payload = build_openai_response(request, full_content, response_tool_calls)
    log_debug_payload(
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
                "created": int(datetime.now().timestamp()),
                "owned_by": "merlin",
            }
            for model in models
        ],
    }
