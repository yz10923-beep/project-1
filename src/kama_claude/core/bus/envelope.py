"""JSON-RPC 2.0 envelopes. Every message on the wire is exactly one of these."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Standard JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

RequestId = str | int


class JsonRpcRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jsonrpc: Literal["2.0"] = "2.0"
    # Notifications (no id) are not supported: every request expects a response.
    id: RequestId
    method: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


class JsonRpcSuccess(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: RequestId
    result: Any


class JsonRpcErrorObject(BaseModel):
    code: int
    message: str
    data: Any = None


class JsonRpcError(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    # None when the request could not be parsed far enough to recover its id.
    id: RequestId | None = None
    error: JsonRpcErrorObject


def make_error(id: RequestId | None, code: int, message: str, data: Any = None) -> JsonRpcError:
    """Build an error response."""
    return JsonRpcError(id=id, error=JsonRpcErrorObject(code=code, message=message, data=data))
