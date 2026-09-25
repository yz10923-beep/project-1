"""Server-pushed events. A discriminated union on `type` so clients can
dispatch on one field and pydantic rejects unknown event shapes.

S0 only defines the union; S2 starts pushing events over IPC.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter


class CoreStartedEvent(BaseModel):
    type: Literal["core.started"] = "core.started"
    version: str
    listen: str
    at: datetime


class CoreStoppingEvent(BaseModel):
    type: Literal["core.stopping"] = "core.stopping"
    reason: str
    at: datetime


Event = Annotated[CoreStartedEvent | CoreStoppingEvent, Field(discriminator="type")]

EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)
