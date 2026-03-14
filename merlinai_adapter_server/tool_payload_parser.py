import json
import re
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

from json_repair import repair_json

from .protocol_constants import STRUCTURED_PAYLOAD_END, STRUCTURED_PAYLOAD_START


def extract_structured_payload_blocks(raw_text: str) -> List[str]:
    if not raw_text:
        return []

    pattern = re.compile(
        re.escape(STRUCTURED_PAYLOAD_START) + r"(.*?)" + r"(?:" +
        re.escape(STRUCTURED_PAYLOAD_END) + r"|" +
        re.escape(STRUCTURED_PAYLOAD_END.replace("/", r"\/")) +
        r")",
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
            try:
                parsed = repair_json(block, return_objects=True)
            except Exception:
                parsed = None
        if isinstance(parsed, dict):
            parsed_objects.append(parsed)
    return parsed_objects


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


def filter_allowed_tool_calls(response_tool_calls: List[Dict[str, Any]], allowed_tool_names: Set[str]) -> List[Dict[str, Any]]:
    return [
        tool_call
        for tool_call in response_tool_calls
        if tool_call.get("function", {}).get("name") in allowed_tool_names
    ]


def resolve_payload_result(raw_text: str, allowed_tool_names: Set[str]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    selected_tool_calls: List[Dict[str, Any]] = []
    selected_message_content: Optional[str] = None

    for payload in reversed(try_parse_structured_payloads(raw_text)):
        if not selected_tool_calls:
            selected_tool_calls = extract_tool_calls_from_json_payload(payload, allowed_tool_names)
        if selected_message_content is None:
            selected_message_content = _extract_message_content_from_payload(payload)
        if selected_tool_calls and selected_message_content is not None:
            return selected_tool_calls, selected_message_content

    return selected_tool_calls, selected_message_content
