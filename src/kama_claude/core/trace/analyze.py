"""Turn a run's spans into answers: where did the time go, where did the tokens and
money go, which call was slowest, and how the event bus and IPC behaved.

`summarize` is pure (spans in, numbers out) so it can be tested with exact values;
`render` and `to_chrome` only format.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kama_claude.core.plan import PLAN_TOOL_NAMES
from kama_claude.core.trace.span import Span

_TOKEN_KEYS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
)


def load_spans(path: Path) -> list[Span]:
    spans = [Span.model_validate_json(line) for line in path.read_text().splitlines() if line]
    return sorted(spans, key=lambda s: s.start_ns)


@dataclass
class LlmCall:
    step: int
    offset_ms: float
    duration_ms: float
    ttft_ms: float | None
    output_tokens: int
    tokens_per_s: float | None  # output tokens / generation time (after the first token)
    cost_usd: float | None
    stop_reason: str | None


@dataclass
class ContextPoint:
    step: int
    tokens: int  # the request's exact size
    estimate: int | None  # what the meter predicted before sending it


@dataclass
class Compaction:
    step: int
    tokens_before: int
    duration_ms: float
    cost_usd: float | None
    ok: bool


@dataclass
class TraceSummary:
    run_id: str
    status: str
    model: str
    wall_ms: float
    steps: int
    llm_ms: float = 0.0
    tool_ms: float = 0.0
    approval_ms: float = 0.0
    tokens: dict[str, int] = field(default_factory=dict)
    cost_usd: float | None = 0.0
    calls: list[LlmCall] = field(default_factory=list)
    slowest: list[Span] = field(default_factory=list)
    subscriptions: list[dict[str, Any]] = field(default_factory=list)
    ipc: dict[str, dict[str, float]] = field(default_factory=dict)
    # S3: tasks/completed/cancelled/open/reminders from the run span; None if planning off
    plan: dict[str, int] | None = None
    tool_calls: int = 0
    plan_tool_calls: int = 0  # task_* calls: the bookkeeping cost of planning
    # Steps whose only tool calls were task_*: a whole model round-trip spent on the plan.
    plan_only_steps: int = 0
    plan_only_ms: float = 0.0
    task_spans: list[Span] = field(default_factory=list)  # one per task worked on, by id
    # S4 memory: what the run started from and what it left behind
    session_id: str | None = None
    history_messages: int = 0
    memory_notes: int = 0
    memory_chars: int = 0
    notes_changed: int = 0
    # S5: what the policy decided and how long the run waited out model-call failures
    policy_mode: str | None = None
    sandbox: str | None = None
    blocked: int = 0
    repeat_blocked: int = 0
    asked: int = 0
    retries: int = 0
    backoff_ms: float = 0.0
    # S6: how big each request was (exact) beside what the meter predicted, the budget,
    # compactions (not model calls: summaries) and results cut at the cap
    context_budget: int | None = None  # None = KAMA_CONTEXT=false
    compaction_on: bool = False
    context: list[ContextPoint] = field(default_factory=list)
    compactions: list[Compaction] = field(default_factory=list)
    compact_ms: float = 0.0
    cut_results: int = 0
    chars_cut: int = 0

    @property
    def other_ms(self) -> float:
        """Loop overhead: event writes, prompt building, scheduling."""
        busy = self.llm_ms + self.tool_ms + self.approval_ms + self.backoff_ms + self.compact_ms
        return max(0.0, self.wall_ms - busy)

    @property
    def context_peak(self) -> int:
        return max((p.tokens for p in self.context), default=0)

    @property
    def estimate_error(self) -> float | None:
        """Median signed error of the meter's prediction, as a share of the actual size
        (positive = it overestimated, which is the safe side)."""
        errs = [(p.estimate - p.tokens) / p.tokens for p in self.context if p.estimate and p.tokens]
        return statistics.median(errs) if errs else None

    @property
    def cache_hit_ratio(self) -> float | None:
        prompt = (
            self.tokens.get("input_tokens", 0)
            + self.tokens.get("cache_read_input_tokens", 0)
            + self.tokens.get("cache_creation_input_tokens", 0)
        )
        return self.tokens.get("cache_read_input_tokens", 0) / prompt if prompt else None


def summarize(spans: list[Span]) -> TraceSummary:
    runs = [s for s in spans if s.name == "run" and s.kind == "agent"]
    if not runs:
        raise ValueError("no run span in this trace (did the run start?)")
    run = runs[0]
    summary = TraceSummary(
        run_id=run.trace_id,
        status=str(run.attrs.get("status", run.status)),
        model=str(run.attrs.get("model", "?")),
        wall_ms=run.duration_ms,
        steps=int(run.attrs.get("steps", 0)),
        tokens={k: 0 for k in _TOKEN_KEYS},
    )
    summary.session_id = run.attrs.get("session_id")
    summary.history_messages = int(run.attrs.get("history_messages", 0))
    summary.memory_notes = int(run.attrs.get("memory_notes", 0))
    summary.memory_chars = int(run.attrs.get("memory_chars", 0))
    summary.notes_changed = int(run.attrs.get("notes_changed", 0))
    summary.policy_mode = run.attrs.get("policy_mode")
    summary.sandbox = run.attrs.get("sandbox")
    summary.blocked = int(run.attrs.get("policy_denials", 0))
    summary.repeat_blocked = int(run.attrs.get("repeat_denials", 0))
    summary.asked = int(run.attrs.get("approvals_asked", 0))
    if "context_budget" in run.attrs:
        summary.context_budget = int(run.attrs["context_budget"])
        summary.compaction_on = bool(run.attrs.get("context_compaction"))
    if "plan_tasks" in run.attrs:
        summary.plan = {
            k.removeprefix("plan_"): int(v) for k, v in run.attrs.items() if k.startswith("plan_")
        }
    for s in spans:
        if s.name == "llm.backoff":  # waiting before a retry, not model time
            summary.backoff_ms += s.duration_ms
            summary.retries += 1
        elif s.name == "context.compact":  # a summary, billed like a call but not a step's
            summary.compact_ms += s.duration_ms
            for k in _TOKEN_KEYS:
                summary.tokens[k] += int(s.attrs.get(k, 0))
            cost = s.attrs.get("cost_usd")
            summary.cost_usd = (
                None if cost is None or summary.cost_usd is None else summary.cost_usd + cost
            )
            summary.compactions.append(
                Compaction(
                    step=int(s.attrs.get("step", 0)),
                    tokens_before=int(s.attrs.get("tokens_before", 0)),
                    duration_ms=s.duration_ms,
                    cost_usd=cost,
                    ok=s.status == "ok",
                )
            )
        elif s.kind == "llm":
            if "context_tokens" in s.attrs:
                est = s.attrs.get("context_estimate")
                summary.context.append(
                    ContextPoint(
                        step=int(s.attrs.get("step", 0)),
                        tokens=int(s.attrs["context_tokens"]),
                        estimate=int(est) if est is not None else None,
                    )
                )
            summary.llm_ms += s.duration_ms
            for k in _TOKEN_KEYS:
                summary.tokens[k] += int(s.attrs.get(k, 0))
            cost = s.attrs.get("cost_usd")
            summary.cost_usd = (
                None if cost is None or summary.cost_usd is None else summary.cost_usd + cost
            )
            ttft = s.attrs.get("ttft_ms")
            out = int(s.attrs.get("output_tokens", 0))
            gen_ms = s.duration_ms - ttft if ttft is not None else None
            summary.calls.append(
                LlmCall(
                    step=int(s.attrs.get("step", 0)),
                    offset_ms=(s.start_ns - run.start_ns) / 1e6,
                    duration_ms=s.duration_ms,
                    ttft_ms=ttft,
                    output_tokens=out,
                    tokens_per_s=out / (gen_ms / 1000) if gen_ms and gen_ms > 0 else None,
                    cost_usd=cost,
                    stop_reason=s.attrs.get("stop_reason"),
                )
            )
        elif s.name == "tool.exec":
            summary.tool_ms += s.duration_ms
        elif s.name == "tool.approval":
            summary.approval_ms += s.duration_ms
        elif s.kind == "tool":  # the `tool <name>` span around approval + exec
            summary.tool_calls += 1
            if "cut_chars" in s.attrs:
                summary.cut_results += 1
                summary.chars_cut += int(s.attrs["cut_chars"])
            summary.plan_tool_calls += s.attrs.get("tool") in PLAN_TOOL_NAMES
        elif s.kind == "bus":
            summary.subscriptions.append({"duration_ms": s.duration_ms, **s.attrs})
        elif s.kind == "plan":
            summary.task_spans.append(s)
    summary.task_spans.sort(key=lambda s: (int(s.attrs.get("task_id", 0)), s.start_ns))
    tools_by_step: dict[str, list[str]] = defaultdict(list)
    for s in spans:
        if s.kind == "tool" and s.parent_id is not None and "tool" in s.attrs:
            tools_by_step[s.parent_id].append(str(s.attrs["tool"]))
    for s in spans:
        names = tools_by_step.get(s.span_id)
        if s.name.startswith("step ") and names and all(n in PLAN_TOOL_NAMES for n in names):
            summary.plan_only_steps += 1
            summary.plan_only_ms += s.duration_ms
    work = [s for s in spans if s.name == "llm.call" or s.name == "tool.exec"]
    summary.slowest = sorted(work, key=lambda s: s.duration_ns, reverse=True)[:3]

    by_method: dict[str, list[float]] = defaultdict(list)
    for s in spans:
        if s.kind == "ipc":
            by_method[str(s.attrs.get("method", s.name))].append(s.duration_ms)
    summary.ipc = {
        m: {"count": len(d), "p50_ms": statistics.median(d), "max_ms": max(d)}
        for m, d in sorted(by_method.items())
    }
    return summary


def _bar(share: float, width: int = 24) -> str:
    return "█" * max(0, round(share * width))


def _fmt_s(ms: float) -> str:
    return f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms:.0f}ms"


def _depths(spans: list[Span]) -> dict[str, int]:
    parent = {s.span_id: s.parent_id for s in spans}
    depths: dict[str, int] = {}
    for s in spans:
        d, p = 0, s.parent_id
        while p is not None and p in parent:
            d, p = d + 1, parent[p]
        depths[s.span_id] = d
    return depths


def _k(n: float) -> str:
    return f"{n / 1000:.1f}K"


def _render_context(s: TraceSummary, width: int) -> list[str]:
    """S6: the request size per step against the budget, with compactions in place."""
    if not s.context:
        return []
    peak = s.context_peak
    head = [f"budget {_k(s.context_budget)}" if s.context_budget else "governance off"]
    share = f" ({peak / s.context_budget:.0%} of budget)" if s.context_budget else ""
    head.append(f"peak {_k(peak)}{share}")
    if s.compactions:
        failed = sum(not c.ok for c in s.compactions)
        head.append(
            f"{len(s.compactions) - failed} compaction(s)"
            + (f", {failed} failed" if failed else "")
        )
    elif s.context_budget and not s.compaction_on:
        head.append("compaction off for this model")
    if s.cut_results:
        head.append(f"{s.cut_results} result(s) cut ({s.chars_cut:,} chars kept outside)")
    if (err := s.estimate_error) is not None:
        head.append(f"estimate vs actual {err:+.0%} (median)")
    lines = ["", "context  " + " · ".join(head)]
    top = max(peak, s.context_budget or 0) or 1
    mark = round((s.context_budget or 0) / top * width) if s.context_budget else None
    by_step = {c.step: c for c in s.compactions}
    for p in s.context:
        if (c := by_step.pop(p.step, None)) is not None:
            cost = f", ${c.cost_usd:.4f}" if c.cost_usd is not None else ""
            what = "compacted" if c.ok else "compaction FAILED at"
            lines.append(f"  ⇣ {what} {_k(c.tokens_before)} ({_fmt_s(c.duration_ms)}{cost})")
        n = round(p.tokens / top * width)
        row = ["█" if i < n else " " for i in range(width)]
        if mark is not None and mark < width:
            row[mark] = "┃" if mark < n else "┊"
        lines.append(f"  step {p.step:>3} |{''.join(row)}| {_k(p.tokens):>7}")
    return lines


def render(spans: list[Span], width: int = 40) -> str:
    """Human-readable report: breakdown, tokens, waterfall, slowest spans, bus and IPC."""
    s = summarize(spans)
    wall = s.wall_ms or 1.0
    cost = f"${s.cost_usd:.4f}" if s.cost_usd is not None else "cost unknown (unpriced model)"
    lines = [
        f"run {s.run_id} · {s.status} · {s.steps} steps · {_fmt_s(s.wall_ms)} wall · "
        f"{cost} · {s.model}",
        "",
        "where the time went",
    ]
    for label, ms in (
        ("model", s.llm_ms),
        ("tools", s.tool_ms),
        ("approval", s.approval_ms),
        *([("retry wait", s.backoff_ms)] if s.backoff_ms else []),
        *([("compaction", s.compact_ms)] if s.compact_ms else []),
        ("other", s.other_ms),
    ):
        lines.append(f"  {label:<10}{_fmt_s(ms):>7} {ms / wall:>5.0%}  {_bar(ms / wall)}")

    t = s.tokens
    hit = f"{s.cache_hit_ratio:.0%}" if s.cache_hit_ratio is not None else "n/a"
    lines += [
        "",
        f"tokens  in {t['input_tokens']:,} uncached · "
        f"{t['cache_read_input_tokens']:,} cache read · "
        f"{t['cache_creation_input_tokens']:,} cache write · {t['output_tokens']:,} out "
        f"(cache hit {hit})",
    ]
    lines += _render_context(s, width)
    if s.plan is not None:
        p = s.plan
        share = f" ({s.plan_tool_calls / s.tool_calls:.0%} of tool calls)" if s.tool_calls else ""
        lines.append(
            f"plan    {p.get('tasks', 0)} tasks · {p.get('completed', 0)} completed · "
            f"{p.get('cancelled', 0)} cancelled · {p.get('open', 0)} open · "
            f"{p.get('reminders', 0)} reminder(s) · {s.plan_tool_calls} task_* calls{share}"
        )
        if s.plan_only_steps:
            lines.append(
                f"        {s.plan_only_steps} of {s.steps} steps only updated the plan "
                f"({_fmt_s(s.plan_only_ms)}, {s.plan_only_ms / wall:.0%} of wall time)"
                + (
                    f"; {credit} not counted against max_steps"
                    if (credit := (s.plan or {}).get("budget_credit"))
                    else ""
                )
            )
    if s.session_id or s.memory_notes or s.notes_changed:
        where = (
            f"continues session {s.session_id} ({s.history_messages} messages carried in)"
            if s.history_messages
            else f"session {s.session_id} (first run)"
            if s.session_id
            else "no session"
        )
        lines.append(
            f"memory  {where} · {s.memory_notes} note(s) sent "
            f"(~{s.memory_chars // 4} tokens) · {s.notes_changed} note change(s)"
        )
    if s.policy_mode or s.retries:
        bits = [f"mode {s.policy_mode} · sandbox {s.sandbox}" if s.policy_mode else "policy off"]
        if s.blocked:
            again = f" ({s.repeat_blocked} repeated)" if s.repeat_blocked else ""
            bits.append(f"{s.blocked} call(s) blocked{again}")
        if s.asked:
            bits.append(f"{s.asked} asked")
        if s.retries:
            bits.append(f"{s.retries} model retries ({_fmt_s(s.backoff_ms)} waiting)")
        lines.append("safety  " + " · ".join(bits))
    lines += ["", "timeline" + " " * 21 + "|" + "-" * width + "|"]
    run_start = min(x.start_ns for x in spans if x.name == "run")
    depths = _depths(spans)
    for x in spans:
        if x.kind in ("bus", "ipc", "plan") or x.name == "run":
            continue
        start = max(0, (x.start_ns - run_start) / 1e6)
        col = min(width - 1, int(start / wall * width))
        span_w = max(1, int(x.duration_ms / wall * width))
        bar = " " * col + "▇" * min(span_w, width - col)
        label = ("  " * max(0, depths[x.span_id] - 1) + x.name)[:28]
        extra = ""
        if x.kind == "llm":
            ttft = x.attrs.get("ttft_ms")
            extra = f" ttft {_fmt_s(ttft)}" if ttft is not None else ""
            extra += f" out {x.attrs.get('output_tokens', 0)}"
        if x.status != "ok":
            extra += f" [{x.status}]"
        lines.append(f"  {label:<27}|{bar:<{width}}| {_fmt_s(x.duration_ms)}{extra}")

    if s.task_spans:
        lines += ["", "time per task (in_progress -> done)"]
        for x in s.task_spans:
            mark = {"completed": "[x]", "cancelled": "[-]"}.get(str(x.attrs.get("outcome")), "[>]")
            frac = x.duration_ms / wall
            open_ = "  still open at the end" if x.status == "error" else ""
            lines.append(
                f"  {mark} {str(x.attrs.get('task_id', '?')):>2}. "
                f"{str(x.attrs.get('title', ''))[:30]:<30} {_fmt_s(x.duration_ms):>7} "
                f"{frac:>4.0%}  {_bar(frac, 16)}{open_}"
            )
    if s.slowest:
        lines += ["", "slowest"]
        for x in s.slowest:
            where = f"step {x.attrs.get('step', '?')}" if x.kind == "llm" else x.name
            detail = ""
            if x.kind == "llm":
                call = next(c for c in s.calls if c.step == x.attrs.get("step"))
                if call.tokens_per_s:
                    detail = f" · {call.output_tokens} out tok at {call.tokens_per_s:.0f} tok/s"
            lines.append(f"  {_fmt_s(x.duration_ms):>7}  {x.kind:<4} {where}{detail}")
    if s.subscriptions:
        lines += ["", "event bus (one line per client subscription)"]
        for sub in s.subscriptions:
            lines.append(
                f"  {sub.get('client', '?'):<12} {sub.get('ended', '?'):<12} "
                f"replayed {sub.get('replayed', 0)} · live {sub.get('live_events', 0)} "
                f"(deltas {sub.get('deltas', 0)}) · lag mean {sub.get('lag_mean_ms', 0)}ms "
                f"max {sub.get('lag_max_ms', 0)}ms"
            )
    if s.ipc:
        lines += ["", "ipc requests"]
        for method, st in s.ipc.items():
            lines.append(
                f"  {method:<18} ×{int(st['count']):<3} p50 {st['p50_ms']:.2f}ms "
                f"max {st['max_ms']:.2f}ms"
            )
    return "\n".join(lines)


def to_chrome(spans: list[Span]) -> dict[str, Any]:
    """Chrome trace-event JSON: open in https://ui.perfetto.dev or chrome://tracing.
    One row per layer: agent/llm/tool, event bus, IPC."""
    lane = {"agent": 1, "llm": 1, "tool": 1, "bus": 2, "ipc": 3, "plan": 4}
    events: list[dict[str, Any]] = [
        {"name": "thread_name", "ph": "M", "pid": 1, "tid": tid, "args": {"name": name}}
        for tid, name in ((1, "agent"), (2, "event bus"), (3, "ipc"), (4, "plan tasks"))
    ]
    for s in spans:
        events.append(
            {
                "name": s.name,
                "cat": s.kind,
                "ph": "X",  # a complete event: start + duration
                "ts": s.start_ns / 1000,  # microseconds
                "dur": s.duration_ns / 1000,
                "pid": 1,
                "tid": lane[s.kind],
                "args": {**s.attrs, "status": s.status, **({"error": s.error} if s.error else {})},
            }
        )
    return {"traceEvents": events, "displayTimeUnit": "ms"}
