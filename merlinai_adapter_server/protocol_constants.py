from typing import Literal

ToolPromptMode = Literal["default", "strict", "repair"]

STRUCTURED_PAYLOAD_START = "<OPENAI_TOOL_PAYLOAD>"
STRUCTURED_PAYLOAD_END = "</OPENAI_TOOL_PAYLOAD>"
