from typing import Any, List

from fastapi import HTTPException
from pydantic import BaseModel

from .config import TOOL_MESSAGE_MAX_CHARS, TOOL_PROMPT_MAX_MESSAGES
from .schemas import Message


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


def trim_text(value: str, limit: int) -> str:
    if limit <= 0 or len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _message_has_transcript_content(message: Message) -> bool:
    if message.role == "assistant" and message.tool_calls:
        return True
    if message.role == "tool":
        return bool(extract_message_text(message.content) or message.name or message.tool_call_id)
    return bool(extract_message_text(message.content))


def select_tool_prompt_messages(messages: List[Message]) -> List[Message]:
    conversational_messages = [
        message
        for message in messages
        if message.role in {"user", "assistant", "tool"} and _message_has_transcript_content(message)
    ]
    if conversational_messages:
        return conversational_messages[-TOOL_PROMPT_MAX_MESSAGES:]

    non_empty_messages = [message for message in messages if _message_has_transcript_content(message)]
    return non_empty_messages[-TOOL_PROMPT_MAX_MESSAGES:]


def _render_tool_message(message: Message) -> str:
    tool_name = (message.name or "tool").strip() or "tool"
    text = extract_message_text(message.content)
    if text:
        return f"TOOL {tool_name}: {trim_text(text, TOOL_MESSAGE_MAX_CHARS)}"
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
    return f"ASSISTANT: {trim_text(text, TOOL_MESSAGE_MAX_CHARS)}"


def build_conversation_transcript(messages: List[Message]) -> str:
    transcript_parts: List[str] = []
    for message in select_tool_prompt_messages(messages):
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
            transcript_parts.append(f"{message.role.upper()}: {trim_text(text, TOOL_MESSAGE_MAX_CHARS)}")
    return "\n\n".join(transcript_parts)
