import json
from typing import Any, Dict, List, Optional, Set, Union

from fastapi import HTTPException

from .config import TOOL_DESCRIPTION_MAX_CHARS, TOOL_MESSAGE_MAX_CHARS
from .message_utils import build_conversation_transcript, get_last_user_message, select_tool_prompt_messages, trim_text
from .protocol_constants import STRUCTURED_PAYLOAD_END, STRUCTURED_PAYLOAD_START, ToolPromptMode
from .schemas import OpenAIRequest


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
                compact_function["description"] = trim_text(description.strip(), TOOL_DESCRIPTION_MAX_CHARS)

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


def build_tool_prompt(
    request: OpenAIRequest, mode: ToolPromptMode = "default", previous_response: Optional[str] = None
) -> tuple[str, Dict[str, Any]]:
    tool_choice = normalize_tool_choice(request.tool_choice) or "auto"
    compact_tools = compact_tools_for_prompt(request.tools)
    tools_json = json.dumps(compact_tools, ensure_ascii=False, separators=(",", ":"))
    message_transcript = build_conversation_transcript(request.messages)
    if not message_transcript:
        message_transcript = f"USER: {trim_text(get_last_user_message(request.messages), TOOL_MESSAGE_MAX_CHARS)}"
    prompt_parts = _build_tool_prompt_instructions(mode, tool_choice)

    prompt_parts.extend(
        [
            f"Messages:\n{message_transcript}",
            f"Tools:{tools_json}",
        ]
    )
    if previous_response:
        prompt_parts.append(f"Previous invalid response:\n{trim_text(previous_response, TOOL_MESSAGE_MAX_CHARS)}")
    prompt = "\n".join(prompt_parts)

    metrics = {
        "mode": mode,
        "original_message_count": len(request.messages),
        "prompt_message_count": len(select_tool_prompt_messages(request.messages)),
        "original_tools_count": len(request.tools or []),
        "messages_chars": len(message_transcript),
        "tools_chars": len(tools_json),
        "prompt_chars": len(prompt),
    }
    return prompt, metrics


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
