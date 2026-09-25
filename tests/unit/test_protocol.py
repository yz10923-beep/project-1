from __future__ import annotations

import pytest
from pydantic import ValidationError

from kama_claude.core.bus.commands import PingParams, PongResult
from kama_claude.core.bus.envelope import INVALID_PARAMS, JsonRpcRequest, make_error
from kama_claude.core.bus.events import EVENT_ADAPTER, CoreStartedEvent, CoreStoppingEvent


def test_request_roundtrip() -> None:
    req = JsonRpcRequest(id=1, method="core.ping", params={"client": "t"})
    assert JsonRpcRequest.model_validate_json(req.model_dump_json()) == req


def test_request_rejects_wrong_version_and_extra_keys() -> None:
    with pytest.raises(ValidationError):
        JsonRpcRequest.model_validate({"jsonrpc": "1.0", "id": 1, "method": "x"})
    with pytest.raises(ValidationError):
        JsonRpcRequest.model_validate({"jsonrpc": "2.0", "id": 1, "method": "x", "extra": 1})


def test_make_error_shape() -> None:
    err = make_error(7, INVALID_PARAMS, "bad", {"field": "client"})
    assert err.model_dump() == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {"code": -32602, "message": "bad", "data": {"field": "client"}},
    }


def test_ping_params_require_client() -> None:
    with pytest.raises(ValidationError):
        PingParams.model_validate({})
    with pytest.raises(ValidationError):
        PingParams.model_validate({"client": ""})


def test_pong_serializes_datetime_as_iso() -> None:
    pong = PongResult.model_validate(
        {"server_version": "0", "uptime_ms": 1, "received_at": "2026-01-01T00:00:00Z"}
    )
    assert pong.model_dump(mode="json")["received_at"].startswith("2026-01-01T00:00:00")


def test_event_union_dispatches_on_type() -> None:
    started = EVENT_ADAPTER.validate_python(
        {"type": "core.started", "version": "0", "listen": "h:1", "at": "2026-01-01T00:00:00Z"}
    )
    stopping = EVENT_ADAPTER.validate_python(
        {"type": "core.stopping", "reason": "sigterm", "at": "2026-01-01T00:00:00Z"}
    )
    assert isinstance(started, CoreStartedEvent)
    assert isinstance(stopping, CoreStoppingEvent)
    with pytest.raises(ValidationError):
        EVENT_ADAPTER.validate_python({"type": "core.unknown"})
