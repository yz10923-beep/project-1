"""The OS sandbox for bash (S5): what the policy can't see, the kernel still stops.

Backends, best first:
- bwrap (bubblewrap): the filesystem is read-only except the workspace and /tmp; .git and
  .kama inside the workspace stay read-only; the user's credential dirs (~/.ssh, ~/.kama,
  ...) are hidden; its own PID namespace (no killing the daemon); no network unless the
  call was allowed to use it.
- unshare: a network namespace only (no network unless allowed); the filesystem is not
  isolated.
- none: policy only.

Detection runs the backend for real (`true` inside it), because a binary on PATH proves
nothing: Ubuntu 24.04 ships `unshare` but blocks unprivileged user namespaces through
AppArmor, while its bubblewrap package carries an AppArmor profile that allows them.
`kama ping` and run.started report which backend is active, so a missing sandbox is
visible instead of looking like protection.
"""

from __future__ import annotations

import functools
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from kama_claude.core.policy.paths import HOME_SECRETS

SandboxSetting = Literal["auto", "bwrap", "unshare", "off"]
Backend = Literal["bwrap", "unshare", "none"]

# Environment variables that look like credentials never reach the agent's commands.
SECRET_ENV = re.compile(
    r"(API_?KEY|TOKEN|SECRET|PASSW(OR)?D|CREDENTIAL|PRIVATE_KEY|ACCESS_KEY|SESSION_KEY|"
    r"^ANTHROPIC_|^KAMA_|^SSH_AUTH_SOCK$|^GPG_AGENT)",
    re.IGNORECASE,
)


# A new network namespace has only `lo`, and it is down. `ip` isn't always installed, so
# bring it up with the ioctl directly (SIOCGIFFLAGS / SIOCSIFFLAGS, IFF_UP).
_LO_UP = (
    "import fcntl,socket,struct;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);"
    "f=struct.unpack('16sH14s',fcntl.ioctl(s,0x8913,struct.pack('16sH14s',b'lo',0,b'')))[1];"
    "fcntl.ioctl(s,0x8914,struct.pack('16sH14s',b'lo',f|1,b''))"
)


class SandboxUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Sandbox:
    backend: Backend
    note: str = ""  # why this backend: what was probed and what failed

    @property
    def isolates_network(self) -> bool:
        return self.backend in ("bwrap", "unshare")

    @property
    def isolates_files(self) -> bool:
        return self.backend == "bwrap"

    def describe(self) -> str:
        what = {
            "bwrap": "bwrap: read-only system, writable workspace (.git read-only), "
            "credentials hidden, own PID namespace, network only when allowed",
            "unshare": "unshare: network only when allowed; files not isolated",
            "none": "none: policy only (no network or file isolation)",
        }[self.backend]
        return what + (f" ({self.note})" if self.note else "")

    def argv(
        self, command: str, workspace: Path, *, network: bool, hidden: tuple[Path, ...] = ()
    ) -> list[str]:
        """The argv that runs `command` with bash inside this sandbox."""
        inner = ["bash", "-c", command]
        if self.backend == "none":
            return inner
        if self.backend == "unshare":
            if network:
                return inner
            # -r maps us to root inside the namespace, which is what lets us bring up lo
            # (tests that talk to a local server still work); files keep the real owner.
            up = f"{shlex.quote(sys.executable)} -S -I -c {shlex.quote(_LO_UP)} 2>/dev/null"
            return ["unshare", "-rn", "sh", "-c", f'{up}; exec "$@"', "sh", *inner]
        return [*bwrap_args(workspace, network=network, hidden=hidden), "--", *inner]


def bwrap_args(workspace: Path, *, network: bool, hidden: tuple[Path, ...] = ()) -> list[str]:
    ws = str(workspace.resolve())
    home = Path(os.path.expanduser("~")).resolve()
    args = [
        "bwrap",
        "--die-with-parent",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
    ]
    cache = home / ".cache"
    if cache.is_dir():
        args += ["--bind", str(cache), str(cache)]  # pip/uv/pytest caches
    for rel in (*HOME_SECRETS, ".kama"):
        p = home / rel
        if p.is_dir():
            args += ["--tmpfs", str(p)]
        elif p.exists():
            args += ["--ro-bind", "/dev/null", str(p)]
    for p in hidden:
        if p.is_dir():
            args += ["--tmpfs", str(p.resolve())]
        elif p.exists():
            args += ["--ro-bind", "/dev/null", str(p.resolve())]
    args += ["--bind", ws, ws]
    for name in (".git", ".kama"):
        if (workspace / name).exists():
            args += ["--ro-bind", str(workspace / name), str(workspace / name)]
    if not network:
        args.append("--unshare-net")
    args += ["--chdir", ws]
    return args


def _probe(argv: list[str]) -> str | None:
    """None if the command runs, else why not."""
    if shutil.which(argv[0]) is None:
        return f"{argv[0]} is not installed"
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"{argv[0]} failed: {e}"
    if r.returncode != 0:
        return f"{argv[0]} failed: {(r.stderr or r.stdout).strip()[:160]}"
    return None


@functools.cache
def detect(setting: SandboxSetting = "auto") -> Sandbox:
    """The best working backend for `setting`. Raises SandboxUnavailable when a backend
    was asked for explicitly and doesn't work here."""
    if setting == "off":
        return Sandbox("none", "KAMA_SANDBOX=off")
    probe_dir = Path("/tmp")
    bwrap_err = None
    if setting in ("auto", "bwrap"):
        bwrap_err = _probe([*bwrap_args(probe_dir, network=False), "--", "true"])
        if bwrap_err is None:
            return Sandbox("bwrap")
        if setting == "bwrap":
            raise SandboxUnavailable(f"KAMA_SANDBOX=bwrap, but {bwrap_err}")
    unshare_err = _probe(["unshare", "-rn", "true"])
    if unshare_err is None:
        return Sandbox("unshare", f"no bwrap: {bwrap_err}" if bwrap_err else "")
    if setting == "unshare":
        raise SandboxUnavailable(f"KAMA_SANDBOX=unshare, but {unshare_err}")
    return Sandbox("none", "; ".join(e for e in (bwrap_err, unshare_err) if e))


def scrubbed_env(keep: frozenset[str] = frozenset()) -> dict[str, str]:
    """The daemon's environment minus anything that looks like a credential."""
    return {k: v for k, v in os.environ.items() if k in keep or not SECRET_ENV.search(k)}
