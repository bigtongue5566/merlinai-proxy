import datetime
import json
import re
import uuid
from typing import Any, Dict, List, Literal, Optional, Set, Tuple, Union

from fastapi import HTTPException
from pydantic import BaseModel

from .config import TOOL_DESCRIPTION_MAX_CHARS, TOOL_MESSAGE_MAX_CHARS, TOOL_PROMPT_MAX_MESSAGES
from .logging_config import log_debug_payload
from .schemas import Message, OpenAIRequest

ToolPromptMode = Literal["default", "strict", "repair"]

STRUCTURED_PAYLOAD_START = "<OPENAI_TOOL_PAYLOAD>"
STRUCTURED_PAYLOAD_END = "</OPENAI_TOOL_PAYLOAD>"


# Shared request/message helpers used before we hand anything to Merlin.
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


def _trim_text(value: str, limit: int) -> str:
    if limit <= 0 or len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _select_tool_prompt_messages(messages: List[Message]) -> List[Message]:
    conversational_messages = [
        message
        for message in messages
        if message.role in {"user", "assistant", "tool"} and _message_has_transcript_content(message)
    ]
    if conversational_messages:
        return conversational_messages[-TOOL_PROMPT_MAX_MESSAGES:]

    non_empty_messages = [message for message in messages if _message_has_transcript_content(message)]
    return non_empty_messages[-TOOL_PROMPT_MAX_MESSAGES:]


def _message_has_transcript_content(message: Message) -> bool:
    if message.role == "assistant" and message.tool_calls:
        return True
    if message.role == "tool":
        return bool(extract_message_text(message.content) or message.name or message.tool_call_id)
    return bool(extract_message_text(message.content))


def _render_tool_message(message: Message) -> str:
    tool_name = (message.name or "tool").strip() or "tool"
    text = extract_message_text(message.content)
    if text:
        return f"TOOL {tool_name}: {_trim_text(text, TOOL_MESSAGE_MAX_CHARS)}"
    if message.tool_call_id:
        return f"TOOL {tool_name}: tool_call_id={message.tool_call_id}"
    return f"TOOL {tool_name}:"


def _summarize_assistant_message(message: Message) -> str:
    if message.tool_calls:
        tool_names = [
            tool_call.get("function", {}).get("name")
            for tool_call in message.tool_calls
            if isinstance(tool_call, dict)
        ]
        tool_names = [name for name in tool_names if isinstance(name, str) and name]
        if tool_names:
            return f"ASSISTANT TOOL_CALLS: {', '.join(tool_names)}"
    text = extract_message_text(message.content)
    return f"ASSISTANT: {_trim_text(text, TOOL_MESSAGE_MAX_CHARS)}"


def build_conversation_transcript(messages: List[Message]) -> str:
    transcript_parts: List[str] = []
    for message in _select_tool_prompt_messages(messages):
        if message.role == "tool":
            transcript_parts.append(_render_tool_message(message))
            continue
        if message.role == "assistant":
            summary = _summarize_assistant_message(message)
            if summary:
                transcript_parts.append(summary)
            continue

        text = extract_message_text(message.content)
        if text:
            transcript_parts.append(f"{message.role.upper()}: {_trim_text(text, TOOL_MESSAGE_MAX_CHARS)}")
    return "\n\n".join(transcript_parts)


# Tool schemas are compacted before sending them upstream to keep prompts short.
def _compact_tool_parameters(parameters: Any) -> Any:
    if not isinstance(parameters, dict):
        return None

    compact: Dict[str, Any] = {}
    schema_type = parameters.get("type")
    if isinstance(schema_type, str) and schema_type:
        compact["type"] = schema_type

    required = parameters.get("required")
    if isinstance(required, list) and required:
        compact["required"] = [item for item in required if isinstance(item, str)]

    properties = parameters.get("properties")
    if isinstance(properties, dict) and properties:
        compact_properties: Dict[str, Any] = {}
        for name, raw_property in properties.items():
            if not isinstance(name, str) or not isinstance(raw_property, dict):
                continue

            property_payload: Dict[str, Any] = {}
            property_type = raw_property.get("type")
            if isinstance(property_type, str) and property_type:
                property_payload["type"] = property_type

            items = raw_property.get("items")
            compact_items = _compact_tool_parameters(items)
            if compact_items:
                property_payload["items"] = compact_items

            enum_values = raw_property.get("enum")
            if isinstance(enum_values, list) and enum_values:
                property_payload["enum"] = enum_values

            nested_properties = raw_property.get("properties")
            if isinstance(nested_properties, dict) and nested_properties:
                nested_payload = _compact_tool_parameters(raw_property)
                if isinstance(nested_payload, dict):
                    property_payload.update(nested_payload)

            if property_payload:
                compact_properties[name] = property_payload

        if compact_properties:
            compact["properties"] = compact_properties

    return compact or None


def compact_tools_for_prompt(tools: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    compact_tools: List[Dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue

        compact_tool: Dict[str, Any] = {"type": tool.get("type", "function")}
        function_payload = tool.get("function")
        if isinstance(function_payload, dict):
            compact_function: Dict[str, Any] = {}
            name = function_payload.get("name")
            if isinstance(name, str) and name:
                compact_function["name"] = name

            description = function_payload.get("description")
            if isinstance(description, str) and description.strip():
                compact_function["description"] = _trim_text(description.strip(), TOOL_DESCRIPTION_MAX_CHARS)

            parameters = _compact_tool_parameters(function_payload.get("parameters"))
            if parameters:
                compact_function["parameters"] = parameters

            if compact_function:
                compact_tool["function"] = compact_function

        compact_tools.append(compact_tool)

    return compact_tools


def get_allowed_tool_names(request: OpenAIRequest) -> Set[str]:
    allowed_names: Set[str] = set()
    for tool in request.tools or []:
        if not isinstance(tool, dict):
            continue

        function_payload = tool.get("function")
        if not isinstance(function_payload, dict):
            continue

        name = function_payload.get("name")
        if isinstance(name, str) and name:
            allowed_names.add(name)

    return allowed_names


def _build_tool_prompt_instructions(mode: ToolPromptMode, tool_choice: str) -> List[str]:
    payload_schema = (
        '{"type":"tool_calls","tool_calls":[{"name":"tool_name","arguments":{}}]} '
        'or {"type":"message","content":"final answer"}'
    )
    base_instructions = [
        "Return exactly one payload block and nothing else.",
        f"Block format: {STRUCTURED_PAYLOAD_START}{payload_schema}{STRUCTURED_PAYLOAD_END}",
        f"tool_choice={tool_choice}",
        "Use tool_calls when the request needs an available tool.",
        "Use message only when no tool is needed.",
        "Every tool_calls.name must exactly match a tool name from Tools.",
        "arguments must be a JSON object.",
        "No markdown. No explanation outside the payload block.",
        "Examples:",
        'User asks to read a file and read exists -> '
        f'{STRUCTURED_PAYLOAD_START}{{"type":"tool_calls","tool_calls":[{{"name":"read","arguments":{{"filePath":"notes.txt"}}}}]}}{STRUCTURED_PAYLOAD_END}',
        'User asks a normal question with no tool needed -> '
        f'{STRUCTURED_PAYLOAD_START}{{"type":"message","content":"final answer"}}{STRUCTURED_PAYLOAD_END}',
    ]

    if tool_choice == "required" or tool_choice.startswith("function:"):
        base_instructions.insert(3, "You must return tool_calls, not message.")

    if mode == "repair":
        return [
            "Your previous answer was invalid.",
            "Re-emit only one valid payload block.",
            "Do not explain or apologize.",
            f"Allowed JSON schema: {payload_schema}",
            f"Output must start with {STRUCTURED_PAYLOAD_START} and end with {STRUCTURED_PAYLOAD_END}.",
        ]

    return base_instructions


# This is the only prompt contract Merlin gets for tool mode.
def build_tool_prompt(
    request: OpenAIRequest, mode: ToolPromptMode = "default", previous_response: Optional[str] = None
) -> Tuple[str, Dict[str, Any]]:
    tool_choice = normalize_tool_choice(request.tool_choice) or "auto"
    compact_tools = compact_tools_for_prompt(request.tools)
    tools_json = json.dumps(compact_tools, ensure_ascii=False, separators=(",", ":"))
    message_transcript = build_conversation_transcript(request.messages)
    if not message_transcript:
        message_transcript = f"USER: {_trim_text(get_last_user_message(request.messages), TOOL_MESSAGE_MAX_CHARS)}"
    prompt_parts = _build_tool_prompt_instructions(mode, tool_choice)

    prompt_parts.extend(
        [
            f"Messages:\n{message_transcript}",
            f"Tools:{tools_json}",
        ]
    )
    if previous_response:
        prompt_parts.append(f"Previous invalid response:\n{_trim_text(previous_response, TOOL_MESSAGE_MAX_CHARS)}")
    prompt = "\n".join(prompt_parts)

    metrics = {
        "mode": mode,
        "original_message_count": len(request.messages),
        "prompt_message_count": len(_select_tool_prompt_messages(request.messages)),
        "original_tools_count": len(request.tools or []),
        "messages_chars": len(message_transcript),
        "tools_chars": len(tools_json),
        "prompt_chars": len(prompt),
    }
    return prompt, metrics


# Retry is only based on protocol failures, not on semantic guesses.
def should_retry_tool_response(request: OpenAIRequest, exc: HTTPException) -> bool:
    if not should_force_tool_json(request) or exc.status_code != 422:
        return False

    detail = exc.detail if isinstance(exc.detail, str) else ""
    if detail == "Tool mode was enabled, but upstream did not return a valid structured JSON payload.":
        return True
    if detail == "Tool-capable request was answered with plain text instead of a tool call payload.":
        return True
    if detail == "Tool calling was required, but upstream did not return a valid tool call payload.":
        return True
    return isinstance(detail, str) and detail.startswith("Specific tool call was required (")


# Structured payload parsing is intentionally strict: the block must contain valid JSON.
def extract_structured_payload_blocks(raw_text: str) -> List[str]:
    if not raw_text:
        return []

    pattern = re.compile(
        re.escape(STRUCTURED_PAYLOAD_START) + r"(.*?)" + r"(?:"
        + re.escape(STRUCTURED_PAYLOAD_END)
        + r"|"
        + re.escape(STRUCTURED_PAYLOAD_END.replace("/", r"\/"))
        + r")",
        re.DOTALL,
    )
    blocks: List[str] = []
    for match in pattern.finditer(raw_text):
        block = match.group(1).strip()
        if block:
            blocks.append(block)
    return blocks


def try_parse_structured_payloads(raw_text: str) -> List[Dict[str, Any]]:
    parsed_objects: List[Dict[str, Any]] = []
    for block in extract_structured_payload_blocks(raw_text):
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            parsed_objects.append(parsed)
    return parsed_objects


# Merlin can emit tool calls in streaming events or inside the structured payload block.
def extract_tool_calls(inner_data: Dict[str, Any], allowed_tool_names: Optional[Set[str]] = None) -> List[Dict[str, Any]]:
    raw_tool_calls = inner_data.get("toolCalls") or inner_data.get("tool_calls") or []
    if not isinstance(raw_tool_calls, list):
        return []

    normalized: List[Dict[str, Any]] = []
    for call in raw_tool_calls:
        if not isinstance(call, dict):
            continue

        function_payload = call.get("function")
        if not isinstance(function_payload, dict):
            continue

        function_name = function_payload.get("name")
        function_arguments = function_payload.get("arguments")

        if not isinstance(function_name, str) or not function_name:
            continue
        if allowed_tool_names is not None and function_name not in allowed_tool_names:
            continue

        if isinstance(function_arguments, dict):
            function_arguments = json.dumps(function_arguments, ensure_ascii=False)
        elif not isinstance(function_arguments, str):
            continue

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


def extract_tool_calls_from_json_payload(
    payload: Optional[Dict[str, Any]], allowed_tool_names: Optional[Set[str]] = None
) -> List[Dict[str, Any]]:
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
        if allowed_tool_names is not None and name not in allowed_tool_names:
            continue
        if not isinstance(arguments, dict):
            continue

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


def _extract_message_content_from_payload(payload: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(payload, dict):
        return None

    if payload.get("type") == "message" and isinstance(payload.get("content"), str):
        return payload["content"]
    return None


def _filter_allowed_tool_calls(
    response_tool_calls: List[Dict[str, Any]], allowed_tool_names: Set[str]
) -> List[Dict[str, Any]]:
    return [
        tool_call
        for tool_call in response_tool_calls
        if tool_call.get("function", {}).get("name") in allowed_tool_names
    ]


def _resolve_payload_result(raw_text: str, allowed_tool_names: Set[str]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    selected_tool_calls: List[Dict[str, Any]] = []
    selected_message_content: Optional[str] = None
    payload_sources: List[List[Dict[str, Any]]] = [try_parse_structured_payloads(raw_text)]

    for payloads in payload_sources:
        for payload in reversed(payloads):
            # The last valid block wins if the model produced more than one.
            if not selected_tool_calls:
                selected_tool_calls = extract_tool_calls_from_json_payload(payload, allowed_tool_names)
            if selected_message_content is None:
                selected_message_content = _extract_message_content_from_payload(payload)
            if selected_tool_calls and selected_message_content is not None:
                return selected_tool_calls, selected_message_content

    return selected_tool_calls, selected_message_content


def has_successful_tool_result(messages: List[Any]) -> bool:
    success_markers = (
        "wrote file successfully",
        "file written successfully",
        "edited file successfully",
        "tool completed successfully",
    )
    for message in messages:
        text = extract_message_text(getattr(message, "content", None)).lower()
        if any(marker in text for marker in success_markers):
            return True
    return False


def is_unhelpful_followup_message(content: Optional[str]) -> bool:
    if not isinstance(content, str):
        return False

    normalized = " ".join(content.lower().split())
    fallback_markers = (
        "i need a task to work on",
        "what would you like me to do",
        "how can i help you today",
    )
    return any(marker in normalized for marker in fallback_markers)


# Response building turns Merlin output back into an OpenAI-compatible shape.
def _build_response_message(
    request: OpenAIRequest,
    full_content: str,
    selected_message_content: Optional[str],
    all_tool_calls: List[Dict[str, Any]],
) -> Tuple[Dict[str, Any], str]:
    response_message: Dict[str, Any] = {"role": "assistant", "content": selected_message_content or full_content or None}
    finish_reason = "stop"

    if all_tool_calls:
        response_message["content"] = None
        response_message["tool_calls"] = all_tool_calls
        finish_reason = "tool_calls"
    elif has_successful_tool_result(request.messages) and is_unhelpful_followup_message(response_message.get("content")):
        response_message["content"] = "Done."

    return response_message, finish_reason


def _validate_response_mode(
    request: OpenAIRequest,
    force_tool_json: bool,
    all_tool_calls: List[Dict[str, Any]],
    selected_message_content: Optional[str],
    finish_reason: str,
) -> None:
    required_tool_call = normalize_tool_choice(request.tool_choice)
    if force_tool_json and not all_tool_calls and selected_message_content is None:
        raise HTTPException(
            status_code=422,
            detail="Tool mode was enabled, but upstream did not return a valid structured JSON payload.",
        )
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


def build_openai_response(request: OpenAIRequest, full_content: str, response_tool_calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    force_tool_json = should_force_tool_json(request)
    allowed_tool_names = get_allowed_tool_names(request)
    payload_tool_calls, selected_message_content = (
        _resolve_payload_result(full_content, allowed_tool_names) if force_tool_json else ([], None)
    )
    filtered_response_tool_calls = _filter_allowed_tool_calls(response_tool_calls, allowed_tool_names)
    all_tool_calls = filtered_response_tool_calls or payload_tool_calls
    response_message, finish_reason = _build_response_message(
        request, full_content, selected_message_content, all_tool_calls
    )
    _validate_response_mode(
        request,
        force_tool_json,
        all_tool_calls,
        selected_message_content,
        finish_reason,
    )

    return {
        "id": f"chatcmpl-{uuid.uuid4()}",
        "object": "chat.completion",
        "created": int(datetime.datetime.now().timestamp()),
        "model": request.model,
        "choices": [{"index": 0, "message": response_message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


# Streaming reuses the non-stream response shape, then emits it as SSE chunks.
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
