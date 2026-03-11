import datetime
import json
import uuid
from typing import Any, Dict, List, Optional, Union

from fastapi import HTTPException
from pydantic import BaseModel

from .logging_config import log_debug_payload
from .schemas import Message, OpenAIRequest


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
    log_debug_payload(
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
