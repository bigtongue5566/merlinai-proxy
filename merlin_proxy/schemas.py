from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict


class ContentPart(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: Optional[str] = None
    text: Optional[str] = None
    input_text: Optional[str] = None
    content: Optional[str] = None


class Message(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: str
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    content: Optional[Union[str, List[Union[ContentPart, Dict[str, Any], str]]]] = None


class OpenAIRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str
    messages: List[Message]
    stream: Optional[bool] = False
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None
