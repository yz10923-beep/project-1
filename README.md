# kama-claude (from scratch)

A mini local coding-agent runtime in Python, built stage by stage after
[KamaClaude](https://github.com/youngyangyang04/KamaClaude) (MIT). A `kama-core` daemon
owns agent state; the `kama` CLI talks to it over JSON-RPC 2.0 / NDJSON on TCP.

Status: **S0**, meaning the daemon, CLI and typed protocol. See [docs/ROADMAP.md](docs/ROADMAP.md).

```bash
uv sync
uv run kama-core &          # listens on 127.0.0.1:7437
uv run kama ping            # pong server=0.0.1 uptime=1491ms latency=0.6ms
make verify                 # lint + mypy --strict + tests
```

Talk to the daemon by hand:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"core.ping","params":{"client":"nc"}}' | nc -q1 127.0.0.1 7437
```
