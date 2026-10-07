"""A conversation's messages, rebuilt from run events (S4 sessions).

One run's events.jsonl records everything the model was sent: the goal (and any memory
preamble) in run.started, each response in llm.response, tool results in tool.finished,
and the loop's own user turns (plan.reminder, plan.notice). `replay` folds them back into
the exact message list. The loop builds its messages with the same helpers
(`start_run_messages`), so what a session resumes from can't drift from what was sent.

A session's history is the replay of its runs in order, each starting from the previous
one's messages. Two things can break that chain, and both are repaired here:
- a run that ended mid-tool (cancelled, crashed) leaves tool_use blocks with no
  tool_result, which the API rejects: they get an is_error result saying so;
- a run that ended on a user turn (max_steps after tools ran, an API error) would put
  two user turns in a row: the next run's opening blocks join that turn instead.
"""

from __future__ import annotations

import copy
from typing import Any

from kama_claude.core.bus.events import (
    ContextCompactedEvent,
    Event,
    LLMResponseEvent,
    PlanNoticeEvent,
    PlanReminderEvent,
    RunStartedEvent,
    ToolFinishedEvent,
)
from kama_claude.core.llm.types import Message

INTERRUPTED_RESULT = (
    "Not run: the previous run ended before this tool call finished "
    "(cancelled or failed). Its effects, if any, are unknown."
)


def _blocks(content: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"type": "text", "text": content}] if isinstance(content, str) else list(content)


def repair_orphans(messages: list[Message]) -> tuple[list[Message], int]:
    """Give every tool_use of the last assistant turn a tool_result. A run cancelled
    mid-tools may have recorded results for some calls and not others, so the missing
    ones are added to the user turn that follows (or a new one). Returns the repaired
    copy and how many results were added."""
    out = copy.deepcopy(messages)
    last_assistant = next(
        (i for i in range(len(out) - 1, -1, -1) if out[i]["role"] == "assistant"), None
    )
    if last_assistant is None or not isinstance(out[last_assistant]["content"], list):
        return out, 0
    uses = [b["id"] for b in out[last_assistant]["content"] if b.get("type") == "tool_use"]
    after = out[last_assistant + 1] if last_assistant + 1 < len(out) else None
    answered = set()
    if after is not None and isinstance(after["content"], list):
        answered = {
            b.get("tool_use_id") for b in after["content"] if b.get("type") == "tool_result"
        }
    missing = [
        {"type": "tool_result", "tool_use_id": i, "content": INTERRUPTED_RESULT, "is_error": True}
        for i in uses
        if i not in answered
    ]
    if not missing:
        return out, 0
    if after is None:
        out.append({"role": "user", "content": missing})
    else:  # results first, in tool_use order, then whatever text the turn had
        blocks = _blocks(after["content"])
        results = {b["tool_use_id"]: b for b in blocks if b.get("type") == "tool_result"}
        results |= {b["tool_use_id"]: b for b in missing}
        rest = [b for b in blocks if b.get("type") != "tool_result"]
        out[last_assistant + 1] = {"role": "user", "content": [results[i] for i in uses] + rest}
    return out, len(missing)


def append_user(messages: list[Message], content: str | list[dict[str, Any]]) -> None:
    """Add a user turn, joining the previous one if it is also a user turn (blocks after
    its tool_results, which must come first)."""
    if messages and messages[-1]["role"] == "user":
        last = messages[-1]
        messages[-1] = {"role": "user", "content": _blocks(last["content"]) + _blocks(content)}
    else:
        messages.append({"role": "user", "content": content})


def opening_content(goal: str, preamble: str | None) -> str | list[dict[str, Any]]:
    """The run's first user content: the goal, after the memory preamble if there is one.
    Without a preamble it stays a plain string, byte-identical to the S1-S3 request."""
    if not preamble:
        return goal
    return [{"type": "text", "text": preamble}, {"type": "text", "text": goal}]


def start_run_messages(
    history: list[Message], goal: str, preamble: str | None
) -> tuple[list[Message], int]:
    """Messages a run starts from: the session's history (repaired), then the goal.
    Returns (messages, number of tool results added by the repair)."""
    messages, repaired = repair_orphans(history)
    append_user(messages, opening_content(goal, preamble))
    return messages, repaired


RESUME_GOAL_MAX_CHARS = 20_000


def resume_text(goal: str, plan: str | None, read_output: bool) -> str:
    """The user turn after a compaction block (S6). The summary is the server's; the goal
    and the plan are re-stated from the run's own records, verbatim, so the task can't
    drift with the summary."""
    if len(goal) > RESUME_GOAL_MAX_CHARS:
        goal = goal[:RESUME_GOAL_MAX_CHARS] + "\n[... goal cut here; see the summary ...]"
    parts = [
        "<context-restored>",
        "This conversation was compacted: the summary above replaces everything before "
        "this point. The current request, verbatim:",
        f"<goal>\n{goal}\n</goal>",
    ]
    if plan:
        parts.append(f"Your plan, from the run's own records:\n{plan}")
    hint = " Tool results that were cut are still readable with read_output." if read_output else ""
    parts.append(
        "Continue the task from where the summary leaves off. Files on disk are as you left "
        "them: when exact contents matter, check the files rather than relying on the "
        f"summary.{hint}\n</context-restored>"
    )
    return "\n".join(parts)


def compacted_view(block: dict[str, Any], resume: str) -> list[Message]:
    """What the conversation is after a compaction: the block first, as an assistant turn
    of its own (exactly as returned), then the resume turn."""
    return [
        {"role": "assistant", "content": [block]},
        {"role": "user", "content": resume},
    ]


def replay(history: list[Message], events: list[Event]) -> list[Message]:
    """The messages after one run, rebuilt from its events on top of `history`."""
    started = next(e for e in events if isinstance(e, RunStartedEvent))
    messages, _ = start_run_messages(history, started.goal, started.preamble)
    results: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal results
        if results:
            messages.append({"role": "user", "content": results})
            results = []

    for e in events:
        match e:
            case LLMResponseEvent():
                flush()
                messages.append({"role": "assistant", "content": e.content})
            case ToolFinishedEvent():
                block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": e.tool_use_id,
                    "content": e.output,
                }
                if e.is_error:
                    block["is_error"] = True
                results.append(block)
            case PlanReminderEvent():
                flush()
                messages.append({"role": "user", "content": e.text})
            case ContextCompactedEvent():
                flush()
                messages[:] = compacted_view(e.block, e.resume)
            case PlanNoticeEvent():
                flush()
                last = messages[-1]
                messages[-1] = {
                    "role": "user",
                    "content": [*_blocks(last["content"]), {"type": "text", "text": e.text}],
                }
            case _:
                pass
    flush()
    return messages


def _is_compaction_turn(m: Message) -> bool:
    c = m["content"]
    return isinstance(c, list) and bool(c) and c[0].get("type") == "compaction"


def conversation_problems(messages: list[Message]) -> list[str]:
    """Structural rules the Messages API enforces, checked locally: starts with a user
    turn, roles alternate, and every tool_use is answered by a tool_result (same ids,
    results first) in the very next user turn. Empty list = valid."""
    problems = []
    if messages and messages[0]["role"] != "user" and not _is_compaction_turn(messages[0]):
        problems.append("first message is not from the user")
    for i, (a, b) in enumerate(zip(messages, messages[1:], strict=False)):
        if a["role"] == b["role"]:
            problems.append(f"messages {i} and {i + 1} are both {a['role']}")
    for i, m in enumerate(messages):
        if m["role"] != "assistant" or not isinstance(m["content"], list):
            continue
        uses = [x["id"] for x in m["content"] if x.get("type") == "tool_use"]
        if not uses:
            continue
        nxt = messages[i + 1]["content"] if i + 1 < len(messages) else None
        blocks = _blocks(nxt) if nxt is not None else []
        results = [str(x.get("tool_use_id")) for x in blocks if x.get("type") == "tool_result"]
        if sorted(results) != sorted(uses):
            problems.append(f"tool_use {uses} at {i} answered by {results}")
        elif blocks[: len(results)] != [x for x in blocks if x.get("type") == "tool_result"]:
            problems.append(f"tool_results after text at {i + 1}")
    return problems
