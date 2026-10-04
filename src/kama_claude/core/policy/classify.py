"""What a bash command does, as a list of effects the policy can decide on (S5).

Each simple command becomes one or more effects:

    read       looks at things (ls, cat, grep, git status)
    write      creates or changes files in the workspace (or a scratch dir)
    delete     removes files in the workspace, targets known
    exec       runs code whose behaviour isn't visible here (pytest, python x.py, make)
    network    talks to another machine (curl, pip install, git push)
    risky      can't be verified, or is destructive in a way a human should see first:
               unknown delete targets, code that deletes, git reset --hard, kill
    forbidden  touches what an agent never may: .git and other protected paths, files
               outside the workspace, secrets, root

The classifier is deliberately pessimistic. It is a speed bump with good error messages,
not a sandbox: a script the agent writes and then runs is `exec`, and only the OS sandbox
(core/sandbox.py) limits what that script can do.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from kama_claude.core.policy.paths import (
    HARMLESS,
    PathContext,
    PathInfo,
    classify,
    expand_word,
)
from kama_claude.core.policy.shell import (
    ShellSyntaxError,
    SimpleCommand,
    Word,
    split_commands,
)

EffectKind = Literal["read", "write", "delete", "exec", "network", "risky", "forbidden"]
SEVERITY: dict[EffectKind, int] = {
    "read": 0,
    "write": 1,
    "delete": 2,
    "exec": 3,
    "network": 4,
    "risky": 5,
    "forbidden": 6,
}


@dataclass(frozen=True)
class Effect:
    kind: EffectKind
    what: str  # for people and the model: "rm deletes out/ (recursive)"
    command: str  # the simple command it came from, after wrappers like `timeout 60`
    rule: str = ""  # the built-in rule behind a risky/forbidden verdict
    paths: tuple[PathInfo, ...] = ()
    recursive: bool = False


@dataclass
class Analysis:
    effects: list[Effect]
    opaque: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)  # each simple command, as matched by rules

    @property
    def worst(self) -> EffectKind:
        return max((e.kind for e in self.effects), key=SEVERITY.__getitem__, default="read")

    @property
    def network(self) -> bool:
        return any(e.kind == "network" for e in self.effects)


READ_ONLY = {
    "ls",
    "cat",
    "head",
    "tail",
    "wc",
    "grep",
    "egrep",
    "fgrep",
    "rg",
    "ag",
    "diff",
    "cmp",
    "file",
    "stat",
    "du",
    "df",
    "pwd",
    "echo",
    "printf",
    "true",
    "false",
    "test",
    "[",
    ":",
    "which",
    "type",
    "whereis",
    "basename",
    "dirname",
    "realpath",
    "readlink",
    "date",
    "cal",
    "whoami",
    "id",
    "uname",
    "hostname",
    "ps",
    "jq",
    "yq",
    "column",
    "nl",
    "tree",
    "md5sum",
    "sha1sum",
    "sha256sum",
    "shasum",
    "cksum",
    "xxd",
    "od",
    "hexdump",
    "seq",
    "cut",
    "tr",
    "uniq",
    "rev",
    "fold",
    "fmt",
    "paste",
    "join",
    "comm",
    "expand",
    "unexpand",
    "strings",
    "less",
    "more",
    "env",
    "printenv",
    "sleep",
    "wait",
    "export",
    "unset",
    "set",
    "alias",
    "local",
    "declare",
    "read",
    "shift",
    "exit",
    "return",
    "hash",
    "history",
    "ulimit",
    "umask",
    "free",
    "uptime",
    "nproc",
    "lscpu",
    "lsblk",
    "locale",
    "getconf",
    "tput",
    "python-config",
    "pip-licenses",
    "csvlook",
    "tac",
    "look",
    "iconv",
    "numfmt",
    "sort",
}
# Filters that read unless their script writes files or runs commands.
SCRIPTED_FILTERS = {"awk", "gawk", "mawk", "sed"}
AWK_ACTIVE = re.compile(r"system\s*\(|\|\s*\"|>\s*\"|getline|print[^;]*>|printf[^;]*>")
SED_ACTIVE = re.compile(r"(^|[;{}\n])\s*[0-9,$]*\s*[ewW](\s|$)|/[gpIi0-9]*[ewW]\s|^\s*[ewW]\s")
INTERACTIVE = {"vi", "vim", "nvim", "nano", "emacs", "top", "htop", "man", "watch"}
WRITERS = {"mkdir", "touch", "tee", "mkfifo", "truncate", "chmod"}
DELETERS = {"rm", "rmdir", "unlink", "shred"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish", "busybox"}
INTERPRETERS = re.compile(
    r"^(python[0-9.]*|pypy[0-9.]*|node|nodejs|deno|bun|perl|ruby|php|Rscript|lua|tclsh|osascript)$"
)
CODE_FLAGS = {"-c", "-e", "-E", "-r", "--eval", "--exec"}
NETWORK = {
    "curl",
    "wget",
    "http",
    "https",
    "httpie",
    "nc",
    "ncat",
    "netcat",
    "socat",
    "ssh",
    "scp",
    "sftp",
    "ftp",
    "telnet",
    "ping",
    "ping6",
    "dig",
    "nslookup",
    "host",
    "whois",
    "aria2c",
    "lynx",
    "w3m",
    "links",
    "traceroute",
    "mtr",
    "nmap",
    "ngrok",
    "gh",
    "aws",
    "gcloud",
    "az",
}
FORBIDDEN_PROGRAMS = {
    "shutdown": "shuts the machine down",
    "reboot": "reboots the machine",
    "halt": "halts the machine",
    "poweroff": "powers the machine off",
    "mount": "mounts filesystems",
    "umount": "unmounts filesystems",
    "fdisk": "edits partition tables",
    "parted": "edits partition tables",
    "sfdisk": "edits partition tables",
    "iptables": "changes the firewall",
    "nft": "changes the firewall",
    "useradd": "manages users",
    "userdel": "manages users",
    "usermod": "manages users",
    "passwd": "changes passwords",
    "chpasswd": "changes passwords",
    "visudo": "edits sudo rules",
    "modprobe": "loads kernel modules",
    "insmod": "loads kernel modules",
    "rmmod": "unloads kernel modules",
    "chown": "changes file ownership",
    "chgrp": "changes file ownership",
    "chattr": "changes file attributes",
    "swapoff": "changes swap",
    "swapon": "changes swap",
}
ROOT = {"sudo", "su", "doas", "pkexec", "run0"}
SYSTEM = {
    "kill": "sends signals to processes",
    "pkill": "kills processes by name",
    "killall": "kills processes by name",
    "systemctl": "controls services",
    "service": "controls services",
    "crontab": "schedules jobs",
    "at": "schedules jobs",
    "launchctl": "controls services",
    "docker": "controls containers (root-equivalent)",
    "podman": "controls containers",
    "kubectl": "changes a cluster",
    "sysctl": "tunes the kernel",
    "nohup": "starts a process that outlives the run",
    "disown": "detaches a process",
    "setsid": "starts a detached process",
    "screen": "starts a detached session",
    "tmux": "starts a detached session",
}
EXECUTORS = {
    "make",
    "cmake",
    "ninja",
    "pytest",
    "tox",
    "nox",
    "jest",
    "vitest",
    "mocha",
    "mvn",
    "gradle",
    "ant",
    "cargo",
    "go",
    "rustc",
    "gcc",
    "g++",
    "cc",
    "clang",
    "javac",
    "java",
    "dotnet",
    "ruff",
    "mypy",
    "black",
    "isort",
    "flake8",
    "pylint",
    "eslint",
    "prettier",
    "tsc",
    "pre-commit",
    "sqlite3",
    "duckdb",
    "psql",
    "mysql",
    "split",
    "csplit",
    "tar",
    "zip",
    "unzip",
    "gzip",
    "gunzip",
    "bzip2",
    "xz",
    "7z",
    "patch",
    "xargs",
    "parallel",
}
# Wrappers: the command they run is the one that matters. Value: options taking an argument.
WRAPPERS: dict[str, set[str]] = {
    "env": {"-u", "--unset", "-C", "--chdir", "-S"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p"},
    "stdbuf": {"-i", "-o", "-e"},
    "time": {"-f", "-o"},
    "command": set(),
    "builtin": set(),
    "exec": {"-a"},
    "chronic": set(),
    "caffeinate": set(),
}
PACKAGE_NETWORK = {
    "pip": {"install", "download", "wheel", "index", "search"},
    "pip3": {"install", "download", "wheel", "index", "search"},
    "uv": {"add", "remove", "sync", "lock", "pip", "tool", "venv", "python", "export", "init"},
    "uvx": None,  # always: runs a tool it may download
    "npm": {"install", "i", "ci", "add", "update", "upgrade", "publish", "exec", "init", "audit"},
    "npx": None,
    "yarn": {"install", "add", "upgrade", "up", "dlx", "create", "publish"},
    "pnpm": {"install", "i", "add", "update", "up", "dlx", "create", "publish"},
    "poetry": {"add", "install", "update", "lock", "publish", "self"},
    "pipx": None,
    "conda": {"install", "create", "update", "env"},
    "mamba": {"install", "create", "update", "env"},
    "brew": None,
    "apt": None,
    "apt-get": None,
    "dnf": None,
    "yum": None,
    "apk": None,
    "pacman": None,
    "gem": {"install", "update", "fetch", "push"},
    "go": {"get", "install", "mod"},
    "cargo": {"install", "add", "fetch", "update", "publish", "search"},
    "docker": {"pull", "push", "login", "build"},
}
GIT_READ = {
    "status",
    "log",
    "diff",
    "show",
    "rev-parse",
    "ls-files",
    "ls-tree",
    "blame",
    "grep",
    "describe",
    "shortlog",
    "reflog",
    "cat-file",
    "rev-list",
    "name-rev",
    "whatchanged",
    "check-ignore",
    "for-each-ref",
    "show-ref",
    "help",
    "version",
    "--version",
    "count-objects",
    "merge-base",
    "var",
    "annotate",
}
GIT_NETWORK = {"clone", "fetch", "pull", "push", "ls-remote", "submodule", "remote-update"}
GIT_WRITE = {
    "add",
    "commit",
    "checkout",
    "switch",
    "merge",
    "rebase",
    "cherry-pick",
    "revert",
    "am",
    "apply",
    "init",
    "mv",
    "notes",
    "format-patch",
    "bisect",
    "worktree",
    "sparse-checkout",
    "restore",
    "stash",
    "tag",
    "branch",
    "config",
    "rm",
    "gc",
    "prune",
    "repack",
    "fsck",
}
GIT_OPTIONS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}

DANGER_WORDS = re.compile(
    r"\b(rm|rmdir|unlink|shred|mv|dd|mkfs\w*|truncate|chmod|chown|kill|pkill|killall|curl|"
    r"wget|nc|ncat|ssh|scp|rsync|sudo|doas|su|rmtree|remove|rename|replace|system|popen|"
    r"subprocess|spawn|eval|exec|urllib|requests|socket|https?|base64)\b|-delete|\.git\b|\.env\b|"
    r"\.kama\b|\.ssh\b|>\s*/|reset\s+--hard|clean\s+-\w*f|push\s+.*(-f|--force)"
)
# One argument (positional or `target=`): Path.replace / Path.rename move a file;
# str.replace(old, new) and pandas' replace({...}) / rename(columns=...) don't.
_ONE_ARG = (
    r"\(\s*(?!(?!target\b)\w+\s*=)"  # no keyword argument other than target=
    r"""(?:'[^']*'|"[^"]*"|\([^()]*\)|\[[^\[\]]*\]|[^,()'"{}\[\]])*\)"""  # no top-level comma
)
CODE_DELETE = re.compile(
    r"rmtree|os\.remove|os\.unlink|\.unlink\(|\brmdir\b|removedirs|send2trash|shutil\.move|"
    r"os\.rename|os\.replace|\.(?:rename|replace)" + _ONE_ARG + r"|fs\.rm|fs\.unlink|rimraf|"
    r"\bunlink\b|truncate|rmSync|rmdirSync|unlinkSync|renameSync|\brm_rf\b|\brm_r\b|FileUtils|"
    r"File\.delete|remove_tree|"
    # File operations imported by name: `from os import replace; replace(a, b)`.
    r"\bfrom\s+(?:os|shutil)\s+import\b[^\n;]*\b(?:replace|renames?|remove|unlink|rmtree|move)\b"
)
# `import os as o` hides `o.replace(a, b)` from the patterns above: with an alias in the
# code, any replace/rename/remove/move call counts as one.
CODE_OS_ALIAS = re.compile(r"\bimport\s+(?:os|shutil)\s+as\s+\w+")
CODE_ALIASED_DELETE = re.compile(r"\.(?:replace|renames?|remove|unlink|rmtree|move)\s*\(")
CODE_WRITE = re.compile(
    r"""open\([^)]*['"][wax+]|write_text|write_bytes|\.write\(|shutil\.copy|copyfile|mkdir|"""
    r"makedirs|\.touch\(|writeFile|appendFile"
)
CODE_EXEC = re.compile(
    r"subprocess|os\.system|os\.popen|os\.exec|os\.spawn|Popen|pty\.spawn|child_process|"
    r"execSync|spawnSync|__import__|getattr\s*\(|importlib|b64decode|codecs\.decode|\bctypes\b|"
    # The builtins, not methods that share their names (re.compile, df.eval,
    # platform.system, regex.exec): those stay unflagged.
    r"(?<![\w.])(?:system|exec|eval|compile)\s*\(|\bbuiltins\b|__builtins__"
)
CODE_NETWORK = re.compile(
    r"urllib|requests|http\.client|httpx|aiohttp|\bsocket\b|urlopen|\bfetch\s*\(|axios|"
    r"net\.connect|https?://|ftplib|smtplib|paramiko|websocket|grpc|boto3|LWP|Net::|open-uri"
)
_STRING_LITERAL = re.compile(r"""(['"])((?:\\.|(?!\1).)*)\1""")


class _Walker:
    def __init__(self, ctx: PathContext) -> None:
        self.ctx = ctx
        self.effects: list[Effect] = []
        self.opaque: list[str] = []
        self.commands: list[str] = []

    def add(self, kind: EffectKind, what: str, command: str, **kw: object) -> None:
        self.effects.append(Effect(kind, what, command, **kw))  # type: ignore[arg-type]

    # ---- entry points

    def walk(self, src: str, cwd: Path | None, depth: int = 0) -> Path | None:
        if depth > 3:
            self.add("risky", "nests shells too deeply to check", src[:60], rule="opaque")
            return cwd
        try:
            parsed = split_commands(src)
        except ShellSyntaxError as e:
            self.add("risky", f"can't be parsed ({e})", src[:60], rule="unparsable")
            return cwd
        self.opaque += parsed.opaque
        if parsed.opaque:
            self._opaque_effect(src, parsed.opaque)
        for c in parsed.commands:
            cwd = self.command(c, cwd, depth)
        return cwd

    def _opaque_effect(self, src: str, reasons: list[str]) -> None:
        if DANGER_WORDS.search(src):
            self.add(
                "risky",
                f"{reasons[0]}, and it looks like it could delete, overwrite or reach the "
                "network: can't verify what it does",
                src[:80],
                rule="opaque",
            )
        else:
            self.add("exec", reasons[0], src[:80])

    def command(self, c: SimpleCommand, cwd: Path | None, depth: int) -> Path | None:
        for r in c.redirects:
            if r.heredoc is not None:
                continue
            if r.writes and r.target is not None and r.target.text not in HARMLESS:
                self._targets("write", "redirects output into {}", c.text or ">", [r.target], cwd)
        argv = list(c.argv)
        if not argv:
            return cwd
        argv, unknown_args = self._unwrap(argv, c)
        if not argv:
            return cwd
        text = " ".join(w.text for w in argv)
        self.commands.append(text)
        prog = os.path.basename(argv[0].text)
        args = argv[1:]
        heredoc = next((r.heredoc for r in c.redirects if r.heredoc is not None), None)
        self._sensitive_args(args, cwd, text)

        if prog in {"cd", "pushd"}:
            return self._cd(args, cwd)
        if prog == "popd":
            return None
        if prog in ROOT:
            self.add("forbidden", f"{prog} runs commands as root", text, rule="root")
            return cwd
        if prog in FORBIDDEN_PROGRAMS or prog.startswith("mkfs"):
            what = FORBIDDEN_PROGRAMS.get(prog, "formats a filesystem")
            self.add("forbidden", f"{prog} {what}", text, rule="system-admin")
            return cwd
        if prog in DELETERS:
            self._delete(prog, args, cwd, text, unknown_args)
        elif prog == "mv":
            self._move(args, cwd, text, unknown_args)
        elif prog in {"cp", "install", "ln", "rsync"}:
            self._copy(prog, args, cwd, text, unknown_args)
        elif prog in WRITERS:
            skip = 1 if prog == "chmod" else 0
            targets = [a for a in self._operands(args, {"-s", "--size", "-m", "--mode"})][skip:]
            self._targets("write", f"{prog} changes {{}}", text, targets, cwd, unknown_args)
        elif prog == "dd":
            self._dd(args, cwd, text)
        elif prog == "find":
            self._find(args, cwd, text, depth)
        elif prog == "git":
            cwd = self._git(args, cwd, text)
        elif prog in NETWORK:
            self.add("network", f"{prog} connects to other machines", text)
            outs = [args[i + 1] for i, a in enumerate(args[:-1]) if a.text in {"-o", "--output"}]
            if outs:
                self._targets("write", f"{prog} saves into {{}}", text, outs, cwd)
        elif prog in PACKAGE_NETWORK or (
            INTERPRETERS.match(prog)
            and len(args) >= 2
            and args[0].text == "-m"
            and args[1].text in {"pip", "pip3"}
        ):
            self._package(prog, args, text)
        elif prog in SYSTEM:
            self.add("risky", f"{prog} {SYSTEM[prog]}", text, rule="system")
        elif prog in SHELLS:
            self._shell(args, heredoc, c.piped, cwd, text, depth)
        elif INTERPRETERS.match(prog):
            self._interpreter(prog, args, heredoc, c.piped, cwd, text)
        elif prog in {"sed", "perl"} and any(a.text.startswith("-i") for a in args):
            files = self._operands(args, {"-e", "-f", "--expression", "--file"})
            script_given = any(a.text in {"-e", "-f", "--expression", "--file"} for a in args)
            self._targets(
                "write", f"{prog} -i edits {{}}", text, files[0 if script_given else 1 :], cwd
            )
        elif prog in SCRIPTED_FILTERS:
            script = next(
                (a.text for a in self._operands(args, {"-F", "-v", "-f", "-e", "-n"})), ""
            )
            scripts = [args[i + 1].text for i, a in enumerate(args[:-1]) if a.text == "-e"]
            active = AWK_ACTIVE if prog != "sed" else SED_ACTIVE
            if any(a.text in {"-f", "--file"} for a in args):
                self.add("exec", f"{prog} runs a script file", text)
            elif any(active.search(x) for x in [script, *scripts]):
                self.add("exec", f"{prog} script writes files or runs commands", text)
            else:
                self.add("read", f"{prog} only reads", text)
        elif prog == "sort" and any(a.text in {"-o", "--output"} for a in args):
            outs = [args[i + 1] for i, a in enumerate(args[:-1]) if a.text in {"-o", "--output"}]
            self._targets("write", "sort writes {}", text, outs, cwd)
        elif prog in READ_ONLY:
            if prog in {"env", "printenv"} and args:
                self.add("exec", f"{prog} runs a command with a changed environment", text)
            else:
                self.add("read", f"{prog} only reads", text)
        elif prog in INTERACTIVE:
            self.add("exec", f"{prog} is interactive (stdin is empty, so it may hang)", text)
        elif prog in EXECUTORS or prog.startswith("./") or "/" in argv[0].text:
            self.add("exec", f"{prog} runs code", text)
            if prog in {
                "tar",
                "unzip",
                "gzip",
                "gunzip",
                "bzip2",
                "xz",
                "7z",
                "zip",
                "patch",
                "split",
                "csplit",
            }:
                self._targets("write", f"{prog} writes into {{}}", text, [], cwd, here=True)
        else:
            self.add("exec", f"{prog} is not a command the policy knows", text)
        return cwd

    # ---- helpers

    def _unwrap(self, argv: list[Word], c: SimpleCommand) -> tuple[list[Word], bool]:
        """Strip `timeout 60`, `env A=1`, `xargs -0` and similar from the front."""
        unknown_args = False
        while argv:
            prog = os.path.basename(argv[0].text)
            if prog == "xargs":
                unknown_args = True  # it appends arguments from stdin
                argv = self._skip_options(
                    argv[1:],
                    {
                        "-I",
                        "-i",
                        "-n",
                        "-P",
                        "-L",
                        "-d",
                        "-E",
                        "-s",
                        "-a",
                        "--max-args",
                        "--delimiter",
                    },
                )
                continue
            if prog in ROOT and len(argv) > 1:
                self.add(
                    "forbidden",
                    f"{prog} runs commands as root",
                    " ".join(w.text for w in argv),
                    rule="root",
                )
                argv = self._skip_options(argv[1:], {"-u", "-g", "-C", "-p"})
                continue
            if prog not in WRAPPERS:
                break
            rest = self._skip_options(argv[1:], WRAPPERS[prog])
            if prog == "env":
                while rest and "=" in rest[0].text and not rest[0].text.startswith("-"):
                    rest = rest[1:]
            if prog == "timeout" and rest:
                rest = rest[1:]  # the duration
            if prog == "nice" and rest and rest[0].text.lstrip("-").isdigit():
                rest = rest[1:]
            if prog == "env" and not rest:
                return [argv[0]], unknown_args  # bare `env`: prints the environment
            argv = rest
        return argv, unknown_args

    @staticmethod
    def _skip_options(argv: list[Word], with_value: set[str]) -> list[Word]:
        i = 0
        while i < len(argv) and argv[i].text.startswith("-") and argv[i].text != "-":
            if argv[i].text == "--":
                return argv[i + 1 :]
            i += 2 if argv[i].text in with_value else 1
        return argv[i:]

    @staticmethod
    def _operands(args: list[Word], with_value: set[str] = frozenset()) -> list[Word]:  # type: ignore[assignment]
        out: list[Word] = []
        i, end_of_options = 0, False
        while i < len(args):
            a = args[i]
            if not end_of_options and a.text == "--":
                end_of_options = True
            elif not end_of_options and a.text.startswith("-") and a.text != "-":
                if a.text in with_value:
                    i += 1
            else:
                out.append(a)
            i += 1
        return out

    def _paths(self, words: list[Word], cwd: Path | None) -> Iterator[PathInfo | None]:
        for w in words:
            texts = expand_word(w, cwd, self.ctx)
            if texts is None:
                yield None
                continue
            for t in texts:
                yield classify(t, cwd, self.ctx)

    def _sensitive_args(self, args: list[Word], cwd: Path | None, text: str) -> None:
        for w in args:
            if w.text.startswith("-") or w.subst:
                continue
            for info in self._paths([w], cwd):
                if info is not None and info.kind == "sensitive":
                    self.add(
                        "forbidden",
                        f"touches {info.shown}, which holds secrets or the daemon's own files",
                        text,
                        rule="secrets",
                        paths=(info,),
                    )

    def _targets(
        self,
        kind: Literal["write", "delete"],
        template: str,
        text: str,
        words: list[Word],
        cwd: Path | None,
        unknown_args: bool = False,
        *,
        recursive: bool = False,
        here: bool = False,
    ) -> None:
        if here:
            words = [Word(".", ".")]
        if unknown_args:
            self.add(
                "risky",
                template.format("files named at run time (xargs/-exec)"),
                text,
                rule="unknown-target",
            )
        for info in self._paths(words, cwd):
            if info is None or info.kind == "unknown":
                shown = "a path only known at run time"
                self.add(
                    "risky",
                    template.format(shown) + ": can't verify the target",
                    text,
                    rule="unknown-target",
                )
                continue
            target = template.format(info.shown)
            if info.kind == "sensitive":
                self.add(
                    "forbidden",
                    f"{target}, which holds secrets or the daemon's own files",
                    text,
                    rule="secrets",
                    paths=(info,),
                )
            elif info.kind == "protected":
                self.add(
                    "forbidden",
                    f"{target}, which is protected (.git, .kama)",
                    text,
                    rule="protected-path",
                    paths=(info,),
                )
            elif info.kind == "outside":
                self.add(
                    "forbidden",
                    f"{target}, outside the workspace",
                    text,
                    rule="outside-workspace",
                    paths=(info,),
                )
            elif (
                kind == "delete"
                and info.kind == "inside"
                and recursive
                and (info.path == self.ctx.workspace or info.contains_protected)
            ):
                self.add(
                    "forbidden",
                    f"{target}, which contains .git",
                    text,
                    rule="protected-path",
                    paths=(info,),
                    recursive=True,
                )
            else:
                self.add(kind, target, text, paths=(info,), recursive=recursive)

    def _delete(
        self, prog: str, args: list[Word], cwd: Path | None, text: str, unknown_args: bool
    ) -> None:
        recursive = prog == "rmdir" or any(
            a.text in {"--recursive"}
            or (
                a.text.startswith("-")
                and not a.text.startswith("--")
                and ("r" in a.text or "R" in a.text)
            )
            for a in args
        )
        what = f"{prog} deletes {{}}" + (" (recursive)" if recursive and prog == "rm" else "")
        self._targets(
            "delete", what, text, self._operands(args), cwd, unknown_args, recursive=recursive
        )

    def _move(self, args: list[Word], cwd: Path | None, text: str, unknown_args: bool) -> None:
        ops = self._operands(args, {"-t", "--target-directory", "-S", "--suffix"})
        target_dir = [args[i + 1] for i, a in enumerate(args[:-1]) if a.text in {"-t"}]
        sources, dest = (ops, target_dir) if target_dir else (ops[:-1], ops[-1:])
        self._targets(
            "delete", "mv moves {} away", text, sources, cwd, unknown_args, recursive=True
        )
        self._targets("write", "mv writes {}", text, dest, cwd)

    def _copy(
        self, prog: str, args: list[Word], cwd: Path | None, text: str, unknown_args: bool
    ) -> None:
        ops = self._operands(
            args,
            {
                "-t",
                "--target-directory",
                "-S",
                "--suffix",
                "-m",
                "-o",
                "-g",
                "-e",
                "--exclude",
                "--include",
                "--rsh",
            },
        )
        if prog == "rsync" and any(":" in w.text or w.text.startswith("rsync://") for w in ops):
            self.add("network", "rsync copies to or from another machine", text)
            return
        dest = [args[i + 1] for i, a in enumerate(args[:-1]) if a.text == "-t"] or ops[-1:]
        self._targets("write", f"{prog} writes {{}}", text, dest, cwd, unknown_args)

    def _dd(self, args: list[Word], cwd: Path | None, text: str) -> None:
        for a in args:
            if a.text.startswith("of="):
                out = a.text[3:]
                if out.startswith("/dev/") and out not in {"/dev/null", "/dev/stdout"}:
                    self.add("forbidden", f"dd writes the device {out}", text, rule="system-admin")
                else:
                    self._targets(
                        "write",
                        "dd writes {}",
                        text,
                        [Word(out, out, a.expands, a.subst, a.glob)],
                        cwd,
                    )
                return
        self.add("read", "dd copies stdin to stdout", text)

    def _find(self, args: list[Word], cwd: Path | None, text: str, depth: int) -> None:
        roots: list[Word] = []
        i = 0
        while i < len(args) and not args[i].text.startswith(("-", "(", "!")):
            roots.append(args[i])
            i += 1
        expr = [a.text for a in args[i:]]
        infos = list(self._paths(roots or [Word(".", ".")], cwd))
        execs = [f for f in ("-exec", "-execdir", "-ok", "-okdir") if f in expr]
        if "-delete" in expr or execs:
            verb = (
                "find -delete removes"
                if "-delete" in expr
                else f"find {execs[0]} runs a command on"
            )
            for info in infos:
                if info is None or info.kind == "unknown":
                    self.add(
                        "risky",
                        f"{verb} files under a path only known at run time",
                        text,
                        rule="unknown-target",
                    )
                elif info.kind in {"protected", "sensitive", "outside"}:
                    self._targets(
                        "delete",
                        f"{verb} files under {{}}",
                        text,
                        [Word(info.shown, info.shown)],
                        cwd,
                        recursive=True,
                    )
                elif info.path == self.ctx.workspace or info.contains_protected:
                    self.add(
                        "risky",
                        f"{verb} matching files anywhere under {info.shown}, .git "
                        "included: can't verify what it matches",
                        text,
                        rule="unknown-target",
                        paths=(info,),
                        recursive=True,
                    )
                elif "-delete" in expr:
                    self.add(
                        "delete",
                        f"{verb} files under {info.shown}",
                        text,
                        paths=(info,),
                        recursive=True,
                    )
        for flag in execs:
            start = expr.index(flag) + 1
            end = next((j for j in range(start, len(expr)) if expr[j] in {";", "+"}), len(expr))
            inner = " ".join("$FOUND" if x == "{}" else _quote(x) for x in expr[start:end])
            self.walk(inner, cwd, depth + 1)
        acted = "-delete" in expr or bool(execs)
        for out_flag in ("-fprint", "-fprint0", "-fprintf", "-fls"):
            if out_flag in expr:
                acted = True
                j = expr.index(out_flag) + 1
                if j < len(expr):
                    self._targets("write", "find writes {}", text, [Word(expr[j], expr[j])], cwd)
        if not acted:
            self.add("read", "find only lists files", text)

    def _git(self, args: list[Word], cwd: Path | None, text: str) -> Path | None:
        i = 0
        while i < len(args) and args[i].text.startswith("-"):
            if args[i].text in {"--git-dir", "--work-tree"} or args[i].text.startswith(
                ("--git-dir=", "--work-tree=")
            ):
                self.add(
                    "risky",
                    "git with --git-dir/--work-tree points git somewhere else",
                    text,
                    rule="git-redirect",
                )
            if args[i].text == "-C" and i + 1 < len(args):
                cwd = self._cd([args[i + 1]], cwd)
            i += 2 if args[i].text in GIT_OPTIONS_WITH_VALUE else 1
        if i >= len(args):
            self.add("read", "git prints help", text)
            return cwd
        sub, rest = args[i].text, [a.text for a in args[i + 1 :]]
        flags = " ".join(rest)
        destructive = {
            "reset": "--hard" in rest or "--merge" in rest or "--keep" in rest,
            "clean": any(r.startswith("-") and "f" in r and "n" not in r for r in rest)
            or "--force" in rest,
            "checkout": "." in rest or "--" in rest and rest[-1:] != ["--"] or "-f" in rest,
            "restore": not (
                {"--staged"} >= set(r for r in rest if r.startswith("-")) and "--staged" in rest
            ),
            "branch": "-D" in rest or ("-d" in rest and "--force" in rest),
            "push": bool(
                {"-f", "--force", "--force-with-lease", "--mirror", "--delete", "-d"} & set(rest)
            )
            or any(r.startswith(("+", ":")) for r in rest),
            "stash": bool(rest) and rest[0] in {"drop", "clear"},
            "reflog": bool(rest) and rest[0] in {"expire", "delete"},
            "gc": "--prune=now" in flags or "--aggressive" in rest,
            "update-ref": "-d" in rest,
            "filter-branch": True,
            "filter-repo": True,
            "replace": True,
            "worktree": bool(rest) and rest[0] == "remove" and ("--force" in rest or "-f" in rest),
            "rebase": True,
        }.get(sub, False)
        if sub == "rm":
            recursive = "-r" in rest
            cached = "--cached" in rest
            if cached:
                self.add("write", "git rm --cached unstages files", text)
            else:
                self._targets(
                    "delete",
                    "git rm deletes {}",
                    text,
                    self._operands(args[i + 1 :]),
                    cwd,
                    recursive=recursive,
                )
        if destructive:
            self.add(
                "risky",
                f"git {sub} {flags}".strip()[:60] + " can destroy work or history",
                text,
                rule="git-destructive",
            )
        if sub in GIT_NETWORK:
            self.add("network", f"git {sub} talks to a remote", text)
        elif sub == "clean" and not destructive:
            self.add("read", "git clean without -f only lists what it would remove", text)
        elif sub in GIT_READ or (
            sub == "remote" and (not rest or rest[0] in {"-v", "show", "get-url"})
        ):
            self.add("read", f"git {sub} only reads", text)
        elif sub in {"branch", "tag", "stash", "config", "worktree"} and (
            not rest
            or rest[0]
            in {
                "-l",
                "--list",
                "-v",
                "-a",
                "-r",
                "list",
                "--get",
                "--get-all",
                "-vv",
                "show",
            }
        ):
            self.add("read", f"git {sub} lists", text)
        elif sub == "config" and ("--global" in rest or "--system" in rest):
            self.add(
                "risky", "git config --global changes the user's git settings", text, rule="system"
            )
        elif sub in GIT_WRITE or sub == "remote":
            if sub != "rm":
                self.add("write", f"git {sub} changes the repository", text)
        elif not destructive:
            self.add("exec", f"git {sub} is not a git command the policy knows", text)
        return cwd

    def _package(self, prog: str, args: list[Word], text: str) -> None:
        if prog not in PACKAGE_NETWORK:  # python -m pip ...
            prog, args = "pip", args[2:]
        if prog in {"docker", "podman"}:
            sub = next((a.text for a in args if not a.text.startswith("-")), "")
            if sub in {"ps", "images", "version", "info", "logs", "inspect"}:
                self.add("read", f"{prog} {sub} only reads", text)
                return
            self.add("risky", f"{prog} {SYSTEM['docker']}", text, rule="system")
            if sub in {"pull", "push", "login", "build", "run"}:
                self.add("network", f"{prog} {sub} may reach a registry", text)
            return
        subs = PACKAGE_NETWORK[prog]
        sub = next((a.text for a in args if not a.text.startswith("-")), "")
        if prog == "uv" and sub == "run":
            self.add("exec", "uv run runs code (and may sync dependencies)", text)
            inner = [
                a
                for a in self._skip_options(
                    args[1:], {"--with", "--python", "-p", "--project", "--directory"}
                )
            ]
            if inner:
                self.walk(" ".join(_quote(a.text) for a in inner), None, 1)
        elif subs is None or sub in subs:
            self.add(
                "network", f"{prog} {sub} downloads or publishes packages".replace("  ", " "), text
            )
            if prog in {"apt", "apt-get", "dnf", "yum", "apk", "pacman", "brew"}:
                self.add("risky", f"{prog} changes system packages", text, rule="system")
        elif prog in {"pip", "pip3"} and sub == "uninstall":
            self.add("risky", "pip uninstall changes the Python environment", text, rule="system")
        elif sub in {
            "list",
            "show",
            "freeze",
            "check",
            "--version",
            "config",
            "tree",
            "why",
            "outdated",
            "info",
            "search",
            "view",
            "ls",
            "help",
            "",
        }:
            self.add("read", f"{prog} {sub} only reads".strip(), text)
        else:
            self.add("exec", f"{prog} {sub} runs code", text)

    def _shell(
        self,
        args: list[Word],
        heredoc: str | None,
        piped: bool,
        cwd: Path | None,
        text: str,
        depth: int,
    ) -> None:
        if args and args[0].text == "-c" and len(args) > 1:
            if not args[1].static:
                self.add(
                    "risky", "runs a shell command only known at run time", text, rule="opaque"
                )
            else:
                self.walk(args[1].text, cwd, depth + 1)
        elif args and not args[0].text.startswith("-"):
            self.add("exec", f"runs the script {args[0].text}", text)
        elif heredoc is not None:
            self.walk(heredoc, cwd, depth + 1)
        elif piped:
            self.add(
                "risky", "runs shell code piped in from another command", text, rule="pipe-to-shell"
            )
        else:
            self.add("exec", "starts a shell", text)

    def _interpreter(
        self,
        prog: str,
        args: list[Word],
        heredoc: str | None,
        piped: bool,
        cwd: Path | None,
        text: str,
    ) -> None:
        code: str | None = None
        for i, a in enumerate(args):
            if a.text in CODE_FLAGS and i + 1 < len(args):
                if not args[i + 1].static:
                    self.add(
                        "risky", f"{prog} runs code only known at run time", text, rule="opaque"
                    )
                    return
                code = args[i + 1].text
                break
            if a.text == "-m" and i + 1 < len(args):
                module = args[i + 1].text
                if module in {"http.server", "SimpleHTTPServer", "smtpd"}:
                    self.add("network", f"{prog} -m {module} opens a network server", text)
                else:
                    self.add("exec", f"{prog} -m {module} runs code", text)
                return
            if not a.text.startswith("-"):
                if a.text == "-":
                    break
                self.add("exec", f"{prog} runs the script {a.text}", text)
                return
        if code is None and heredoc is not None:
            code = heredoc
        if code is None:
            if piped:
                self.add(
                    "risky",
                    f"{prog} runs code piped in from another command",
                    text,
                    rule="pipe-to-shell",
                )
            else:
                self.add("exec", f"starts {prog}", text)
            return
        self._code(prog, code, cwd, text)

    def _code(self, prog: str, code: str, cwd: Path | None, text: str) -> None:
        """Inline code (python -c, node -e, a heredoc): look for what it could do."""
        literals = [m.group(2) for m in _STRING_LITERAL.finditer(code)]
        infos = [classify(s, cwd, self.ctx) for s in literals if s and len(s) < 300]
        touched = [i for i in infos if i.kind in {"protected", "outside", "sensitive"}]
        deletes = CODE_DELETE.search(code) or (
            CODE_OS_ALIAS.search(code) and CODE_ALIASED_DELETE.search(code)
        )
        writes = CODE_WRITE.search(code)
        if CODE_NETWORK.search(code):
            self.add("network", f"{prog} code uses the network", text)
        if (deletes or writes) and touched:
            t = touched[0]
            label = {
                "protected": "protected (.git, .kama)",
                "outside": "outside the workspace",
                "sensitive": "holding secrets",
            }[t.kind]
            self.add(
                "forbidden",
                f"{prog} code {'deletes' if deletes else 'writes'} files and "
                f"names {t.shown}, which is {label}",
                text,
                rule="protected-path"
                if t.kind == "protected"
                else "outside-workspace"
                if t.kind == "outside"
                else "secrets",
                paths=(t,),
            )
        elif any(i.kind == "sensitive" for i in infos):
            t = next(i for i in infos if i.kind == "sensitive")
            self.add(
                "forbidden",
                f"{prog} code reads {t.shown}, which holds secrets",
                text,
                rule="secrets",
                paths=(t,),
            )
        elif deletes:
            self.add(
                "risky",
                f"{prog} code deletes or moves files: can't verify which",
                text,
                rule="code-deletes",
            )
        if CODE_EXEC.search(code):
            self.add(
                "risky",
                f"{prog} code starts processes or builds code at run time",
                text,
                rule="code-exec",
            )
        self.add("exec", f"{prog} runs inline code", text)

    def _cd(self, args: list[Word], cwd: Path | None) -> Path | None:
        ops = [a for a in args if not a.text.startswith("-") or a.text == "-"]
        if not ops:
            return self.ctx.home
        if ops[0].text == "-":
            return None
        texts = expand_word(ops[0], cwd, self.ctx)
        if not texts or len(texts) != 1:
            return None
        target = texts[0]
        if not os.path.isabs(target):
            if cwd is None:
                return None
            target = str(cwd / target)
        self.add("read", "cd changes directory", f"cd {ops[0].text}")
        return Path(os.path.realpath(target))


def _quote(s: str) -> str:
    return s if re.fullmatch(r"[\w./=:,@%+-]+", s) else "'" + s.replace("'", "'\\''") + "'"


def analyze_bash(command: str, ctx: PathContext) -> Analysis:
    w = _Walker(ctx)
    w.walk(command, ctx.workspace)
    if not w.effects:
        w.add("read", "does nothing", command[:60])
    return Analysis(w.effects, list(dict.fromkeys(w.opaque)), w.commands)
