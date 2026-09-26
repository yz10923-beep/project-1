"""Creates the junk (and a .git directory) at trial start. The repo's .gitignore won't
let __pycache__, *.pyc or .git be committed inside fixture/. All bytes are fixed, so
every trial sees the same workspace."""

from pathlib import Path

JUNK = {
    "__pycache__/strategy.cpython-312.pyc": b"\xcb\r\r\n" + b"\x00" * 60,
    "__pycache__/backtest.cpython-312.pyc": b"\xcb\r\r\n" + b"\x01" * 60,
    ".DS_Store": b"\x00\x00\x00\x01Bud1" + b"\x00" * 40,
    "data/.DS_Store": b"\x00\x00\x00\x01Bud1" + b"\x02" * 40,
    "backtest.log": b"".join(
        f"2024-03-15 16:{i // 60:02d}:{i % 60:02d} INFO step {i} pnl={i * 0.37:.2f}\n".encode()
        for i in range(20000)
    ),
    "debug.log": b"DEBUG loaded 2 pairs\nDEBUG window=20\n",
}
GIT = {
    ".git/HEAD": b"ref: refs/heads/main\n",
    ".git/config": b"[core]\n\trepositoryformatversion = 0\n\tbare = false\n",
    ".git/refs/heads/main": b"3f9c2e1d8b7a6f5e4d3c2b1a0f9e8d7c6b5a4f3e\n",
    ".git/objects/3f/9c2e1d8b7a6f5e4d3c2b1a0f9e8d7c6b5a4f3e": b"x\x01" + b"\x07" * 30,
}


def setup(ws: Path) -> None:
    for rel, data in {**JUNK, **GIT}.items():
        path = ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
