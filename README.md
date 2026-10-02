# kama-claude (from scratch)

A mini local coding-agent runtime in Python, built stage by stage after
[KamaClaude](https://github.com/youngyangyang04/KamaClaude) (MIT). A `kama-core` daemon
owns agent state; the `kama` CLI talks to it over JSON-RPC 2.0 / NDJSON on TCP.

Status: **S4**. Runs execute in the `kama-core` daemon; `kama run` / `attach` stream them live over JSON-RPC, and any client can answer approval prompts. The agent plans with task tools (you can steer the plan), remembers across runs (sessions and durable notes), and every run is traced (`kama trace`). See [docs/ROADMAP.md](docs/ROADMAP.md).

```bash
uv sync
uv run kama-core &          # listens on 127.0.0.1:7437
uv run kama ping            # pong server=0.0.1 uptime=1491ms latency=0.6ms
make verify                 # lint + mypy --strict + tests

mkdir -p ~/.kama && echo 'ANTHROPIC_API_KEY=sk-ant-...' > ~/.kama/.env && chmod 600 ~/.kama/.env
uv run kama run "add a --verbose flag to cli.py and test it"   # asks before bash/write_file
uv run kama run --detach "..."   # prints a run id; then from any terminal:
uv run kama attach <run-id>      # replays what you missed, then streams live
uv run kama runs                 # live runs, with plan progress (plan 3/7)
uv run kama tui                  # full-screen: start, watch, approve, steer the plan
uv run kama chat                 # a conversation: each line is a run that remembers the last
uv run kama notes list           # what the agent remembers about this workspace
uv run kama plan add <run-id> "write a summary" --after 2   # steer a live run's plan
uv run kama trace                # where the latest run's time, tokens and money went
KAMA_PLANNING=false uv run kama run --local "..."   # the S2 agent: no task tools
jq -c '{seq, type, stop_reason, name}' ~/.kama/runs/<run-id>/events.jsonl
```

Talk to the daemon by hand:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"core.ping","params":{"client":"nc"}}' | nc -q1 127.0.0.1 7437
```
