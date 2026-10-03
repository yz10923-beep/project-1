"""Where a path points, for the policy: inside the workspace, protected, outside, secret.

Paths are resolved the way the command would see them (relative to its current directory,
`~` and $HOME expanded, globs expanded like bash without dotglob) and then followed
through symlinks, so `rm -rf link/` is judged by where `link` really points.
"""

from __future__ import annotations

import fnmatch
import glob
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from kama_claude.core.policy.shell import Word

PathKind = Literal["inside", "scratch", "protected", "outside", "sensitive", "unknown"]

# Inside the workspace: version history, the workspace's own policy, secrets.
PROTECTED_NAMES = (".git", ".kama")
SECRET_FILE_PATTERNS = (".env", ".env.*", "*.pem", "id_rsa*", "id_ed25519*", "*.key")
SECRET_TEMPLATES = {".env.example", ".env.sample", ".env.template", ".env.dist"}
# Under the home directory: credentials of other tools.
HOME_SECRETS = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".kube",
    ".docker/config.json",
    ".netrc",
    ".git-credentials",
    ".config/gcloud",
    ".config/gh",
    ".pypirc",
)
SYSTEM_SECRETS = ("/etc/shadow", "/etc/gshadow", "/etc/sudoers", "/proc/*/environ")
HARMLESS = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/zero", "/dev/urandom"}


@dataclass(frozen=True)
class PathInfo:
    kind: PathKind
    shown: str  # what to call it in a message
    path: Path | None = None  # resolved; None when unknown

    @property
    def contains_protected(self) -> bool:
        """A directory that holds protected data (deleting or moving it destroys that)."""
        return self.path is not None and any((self.path / n).exists() for n in PROTECTED_NAMES)


@dataclass(frozen=True)
class PathContext:
    workspace: Path
    home: Path
    # The daemon's own files (token, runs, notes, policy): the agent must not touch them.
    kama_paths: tuple[Path, ...] = ()

    @classmethod
    def for_workspace(cls, workspace: Path, kama_paths: tuple[Path, ...] = ()) -> PathContext:
        home = Path(os.path.expanduser("~")).resolve()
        default = (home / ".kama",)
        return cls(workspace.resolve(), home, tuple(p.resolve() for p in (*default, *kama_paths)))


def _scratch_dirs() -> tuple[Path, ...]:
    return tuple({Path("/tmp").resolve(), Path(tempfile.gettempdir()).resolve()})


def expand_word(w: Word, cwd: Path | None, ctx: PathContext) -> list[str] | None:
    """The concrete path strings a word stands for, or None if it can't be known here."""
    text = w.text
    if w.subst:
        return None
    if w.expands:
        for var in ("$HOME", "${HOME}"):
            if text == var or text.startswith(var + "/"):
                text = str(ctx.home) + text[len(var) :]
                break
        else:
            for var in ("$PWD", "${PWD}"):
                if cwd is not None and (text == var or text.startswith(var + "/")):
                    text = str(cwd) + text[len(var) :]
                    break
            else:
                if text == "~" or text.startswith("~/"):
                    text = str(ctx.home) + text[1:]
                else:
                    return None
        if "$" in text:
            return None
    if w.glob and not w.quoted and cwd is not None:
        matches = sorted(glob.glob(text, root_dir=None if os.path.isabs(text) else cwd))
        if matches:
            return matches
    return [text]


def classify(path_text: str, cwd: Path | None, ctx: PathContext) -> PathInfo:
    if path_text in HARMLESS:
        return PathInfo("scratch", path_text, Path(path_text))
    if not os.path.isabs(path_text) and cwd is None:
        return PathInfo("unknown", path_text)
    base = Path(path_text) if os.path.isabs(path_text) else (cwd or ctx.workspace) / path_text
    resolved = Path(os.path.realpath(base))
    shown = path_text
    if _is_secret(resolved, ctx):
        return PathInfo("sensitive", shown, resolved)
    if any(resolved == k or resolved.is_relative_to(k) for k in ctx.kama_paths):
        return PathInfo("sensitive", shown, resolved)
    if resolved == ctx.workspace or resolved.is_relative_to(ctx.workspace):
        rel = resolved.relative_to(ctx.workspace)
        if ".git" in rel.parts or (rel.parts and rel.parts[0] == ".kama"):
            return PathInfo("protected", shown, resolved)
        return PathInfo("inside", shown, resolved)
    if ctx.workspace.is_relative_to(resolved):  # an ancestor: deleting it deletes the workspace
        return PathInfo("outside", shown, resolved)
    if resolved.is_relative_to(ctx.home):  # someone's files, even when HOME is under /tmp
        return PathInfo("outside", shown, resolved)
    for scratch in _scratch_dirs():
        if resolved.is_relative_to(scratch) and not _same_tree(resolved, ctx.workspace, scratch):
            return PathInfo("scratch", shown, resolved)
    return PathInfo("outside", shown, resolved)


def _same_tree(p: Path, workspace: Path, scratch: Path) -> bool:
    """A workspace that itself lives in /tmp (an eval trial, a quick experiment) shares its
    top-level temp directory with its siblings: those are someone's files, not scratch."""
    if not workspace.is_relative_to(scratch) or workspace == scratch:
        return False
    top = workspace.relative_to(scratch).parts[0]
    rel = p.relative_to(scratch).parts
    return bool(rel) and rel[0] == top


def _is_secret(p: Path, ctx: PathContext) -> bool:
    if p.name not in SECRET_TEMPLATES and any(
        fnmatch.fnmatch(p.name, pat) for pat in SECRET_FILE_PATTERNS
    ):
        return True
    if p.is_relative_to(ctx.home):
        rel = p.relative_to(ctx.home).as_posix()
        if any(rel == s or rel.startswith(s + "/") for s in HOME_SECRETS):
            return True
    return any(fnmatch.fnmatch(str(p), pat) for pat in SYSTEM_SECRETS)
