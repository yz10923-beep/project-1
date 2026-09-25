"""Command contract: one params model and one result model per JSON-RPC method.

Adding a command = add both models here, register a handler in CoreApp,
and add a client call. The method name is the only routing key.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

PING = "core.ping"


class PingParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client: str = Field(min_length=1, description="Name of the calling client, e.g. 'kama-cli'.")


class PongResult(BaseModel):
    server_version: str
    uptime_ms: int = Field(ge=0)
    received_at: datetime
