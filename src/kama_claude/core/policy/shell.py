"""A conservative bash lexer and parser, for the permission policy (S5).

It does not run anything and does not try to be bash. It splits a command line into simple
commands (argv, environment assignments, redirections) and records everything it cannot
see through: command substitution, expansions whose value is unknown, heredoc bodies fed
to an interpreter, function definitions, `eval`. The policy treats what it cannot see as
unknown, never as safe: anything unparsable becomes an "ask", not an "allow".

    split_commands("cd out && rm -rf *.csv 2>/dev/null; echo $(date)")
    -> [cd out] [rm -rf *.csv] [echo $(date)], with `$(date)` marked as a substitution
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Longest first, so `&&` wins over `&` and `>>` over `>`.
_OPERATORS = (
    "&>>",
    "<<<",
    "<<-",
    "&&",
    "||",
    ";;",
    "|&",
    "&>",
    ">>",
    ">&",
    ">|",
    "<<",
    "<&",
    "<>",
    ";",
    "&",
    "|",
    "<",
    ">",
    "(",
    ")",
    "\n",
)
_OPERATOR_START = set(";&|<>()\n")
CONTROL = {"&&", "||", ";", ";;", "&", "|", "|&", "\n", "(", ")"}
REDIRECTS = {"&>>", "&>", ">>", ">&", ">|", "<&", "<>", "<", ">", "<<", "<<-", "<<<"}
# Words that open or continue a compound command; the command after them is what runs.
RESERVED = {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "!", "{", "}"}
LOOP_HEADERS = {"for", "select", "case", "esac", "in"}


class ShellSyntaxError(ValueError):
    pass


@dataclass
class Word:
    text: str  # after quote removal; substitutions kept literally
    raw: str
    expands: bool = False  # $VAR, ${...}, $((...)), a leading ~: value unknown here
    subst: bool = False  # $(...), `...`, <(...): runs a command we don't see
    glob: bool = False  # an unquoted * ? or [
    quoted: bool = False

    @property
    def static(self) -> bool:
        """Its value is known without running anything (globs still need expanding)."""
        return not (self.expands or self.subst)


@dataclass
class Token:
    kind: str  # "word" | "op"
    value: str
    word: Word | None = None
    fd: str = ""  # the number before a redirection: 2>file
    heredoc: Redirect | None = None  # on a << operator: the redirection, body filled in later


@dataclass
class Redirect:
    op: str
    target: Word | None  # None for a duplication (2>&1, >&-)
    fd: str = ""
    heredoc: str | None = None  # the body, for << and <<-

    @property
    def writes(self) -> bool:
        return self.op in {">", ">>", ">|", "&>", "&>>", "<>"} and self.target is not None


@dataclass
class SimpleCommand:
    argv: list[Word] = field(default_factory=list)
    assigns: list[Word] = field(default_factory=list)
    redirects: list[Redirect] = field(default_factory=list)
    piped: bool = False  # stdin comes from the previous command: `x | sh`

    @property
    def program(self) -> str:
        return self.argv[0].text if self.argv else ""

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.argv)


@dataclass
class ParsedShell:
    commands: list[SimpleCommand]
    opaque: list[str]  # why the parse can't be fully trusted; empty = fully visible
    # `cd` anywhere means later paths depend on where it went
    changes_dir: bool = False


class _Lexer:
    def __init__(self, src: str) -> None:
        self.src = src
        self.i = 0
        self.tokens: list[Token] = []
        self.opaque: list[str] = []
        self._heredocs: list[tuple[Redirect, str, bool]] = []  # waiting for the next newline

    def peek(self, k: int = 0) -> str:
        j = self.i + k
        return self.src[j] if j < len(self.src) else ""

    def run(self) -> list[Token]:
        while self.i < len(self.src):
            c = self.peek()
            if c in " \t\r":
                self.i += 1
            elif c == "\\" and self.peek(1) == "\n":
                self.i += 2  # line continuation
            elif c == "#":
                while self.i < len(self.src) and self.peek() != "\n":
                    self.i += 1
            elif c in "<>" and self.peek(1) == "(":
                self._word()  # process substitution: a word that runs a command
            elif c in _OPERATOR_START:
                self._operator()
            else:
                self._word()
        if self._heredocs:
            self._read_heredoc_bodies()
        return self.tokens

    def _operator(self, fd: str = "") -> None:
        for op in _OPERATORS:
            if self.src.startswith(op, self.i):
                self.i += len(op)
                self.tokens.append(Token("op", op, fd=fd))
                if op == "\n" and self._heredocs:
                    self._read_heredoc_bodies()
                return
        raise ShellSyntaxError(f"unexpected {self.peek()!r}")  # pragma: no cover

    def _read_heredoc_bodies(self) -> None:
        for redirect, delim, strip_tabs in self._heredocs:
            lines: list[str] = []
            while self.i < len(self.src):
                end = self.src.find("\n", self.i)
                line = self.src[self.i : end if end >= 0 else len(self.src)]
                self.i = end + 1 if end >= 0 else len(self.src)
                if (line.lstrip("\t") if strip_tabs else line) == delim:
                    break
                lines.append(line)
            else:
                self.opaque.append(f"heredoc without its {delim!r} terminator")
            redirect.heredoc = "\n".join(lines)
        self._heredocs = []

    def _word(self) -> None:
        start = self.i
        text: list[str] = []
        w = Word("", "")
        at_start = True
        while self.i < len(self.src):
            c = self.peek()
            if c in " \t\r" or (c in _OPERATOR_START and not (c in "<>" and self.peek(1) == "(")):
                break
            if c in "<>" and self.peek(1) == "(":
                text.append(self._balanced(self.i + 1))
                w.subst = True
            elif c == "\\":
                text.append(self.peek(1))
                self.i += 2
                w.quoted = True
            elif c == "'":
                end = self.src.find("'", self.i + 1)
                if end < 0:
                    raise ShellSyntaxError("unterminated single quote")
                text.append(self.src[self.i + 1 : end])
                self.i = end + 1
                w.quoted = True
            elif c == '"':
                self.i += 1
                w.quoted = True
                while True:
                    d = self.peek()
                    if d == "":
                        raise ShellSyntaxError("unterminated double quote")
                    if d == '"':
                        self.i += 1
                        break
                    if d == "\\" and self.peek(1) in ("$", "`", '"', "\\", "\n"):
                        text.append(self.peek(1))
                        self.i += 2
                    elif d in "$`":
                        text.append(self._dollar_or_backtick(w))
                    else:
                        text.append(d)
                        self.i += 1
            elif c in "$`":
                text.append(self._dollar_or_backtick(w))
            else:
                if c in "*?[":
                    w.glob = True
                if c == "~" and at_start:
                    w.expands = True
                text.append(c)
                self.i += 1
            at_start = False
        w.text, w.raw = "".join(text), self.src[start : self.i]
        # `2>file`: digits right before a redirection are its fd, not a word
        if w.raw.isdigit() and self.peek() in ("<", ">") and self.peek(1) != "(":
            self._operator(fd=w.raw)
        else:
            self.tokens.append(Token("word", w.text, word=w))
        self._after_redirect_word()

    def _after_redirect_word(self) -> None:
        """`<< EOF`: the word is the delimiter; the body starts at the next newline."""
        if len(self.tokens) < 2:
            return
        op, delim = self.tokens[-2], self.tokens[-1]
        if op.kind == "op" and op.value in {"<<", "<<-"} and delim.word is not None:
            op.heredoc = Redirect(op.value, delim.word, fd=op.fd)
            self._heredocs.append((op.heredoc, delim.value, op.value == "<<-"))

    def _dollar_or_backtick(self, w: Word) -> str:
        c = self.peek()
        if c == "`":
            end = self.src.find("`", self.i + 1)
            if end < 0:
                raise ShellSyntaxError("unterminated backtick")
            s = self.src[self.i : end + 1]
            self.i = end + 1
            w.subst = True
            return s
        nxt = self.peek(1)
        if nxt == "(":
            if self.peek(2) == "(":  # $(( arithmetic )): an expansion, runs nothing
                w.expands = True
            else:
                w.subst = True
            return self._balanced(self.i + 1)
        if nxt == "{":
            end = self.src.find("}", self.i)
            if end < 0:
                raise ShellSyntaxError("unterminated ${")
            s = self.src[self.i : end + 1]
            self.i = end + 1
            w.expands = True
            return s
        j = self.i + 1
        if nxt and (nxt.isalnum() or nxt in "_@*#?$!-"):
            j += 1
            if nxt.isalpha() or nxt == "_":
                while j < len(self.src) and (self.src[j].isalnum() or self.src[j] == "_"):
                    j += 1
            s = self.src[self.i : j]
            self.i = j
            w.expands = True
            return s
        self.i += 1  # a lone $
        return "$"

    def _balanced(self, open_at: int) -> str:
        """The text from the `(` at `open_at` to its matching `)`, quotes respected."""
        depth, j, quote = 0, open_at, ""
        while j < len(self.src):
            ch = self.src[j]
            if quote:
                if ch == "\\" and quote == '"':
                    j += 1
                elif ch == quote:
                    quote = ""
            elif ch in "'\"":
                quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    s = self.src[self.i : j + 1]
                    self.i = j + 1
                    return s
            j += 1
        raise ShellSyntaxError("unbalanced parentheses")


def tokenize(src: str) -> tuple[list[Token], list[str]]:
    lexer = _Lexer(src)
    return lexer.run(), lexer.opaque


def split_commands(src: str) -> ParsedShell:
    """Parse a command line. Raises ShellSyntaxError when it can't be tokenized at all."""
    tokens, opaque = tokenize(src)
    commands: list[SimpleCommand] = []
    cur = SimpleCommand()
    changes_dir = False
    i = 0

    def flush(piped: bool = False) -> None:
        nonlocal cur
        if cur.argv or cur.redirects or cur.assigns:
            commands.append(cur)
        cur = SimpleCommand(piped=piped)

    while i < len(tokens):
        t = tokens[i]
        if t.kind == "op" and t.value in CONTROL:
            flush(piped=t.value in {"|", "|&"})
            i += 1
            continue
        if t.kind == "op" and t.value in REDIRECTS:
            if t.heredoc is not None:
                cur.redirects.append(t.heredoc)
                i += 2
                continue
            target = tokens[i + 1] if i + 1 < len(tokens) else None
            if target is None or target.kind != "word" or target.word is None:
                raise ShellSyntaxError(f"redirection {t.value!r} without a target")
            w = target.word
            dup = t.value in {">&", "<&"} and (w.text.isdigit() or w.text == "-")
            cur.redirects.append(Redirect(t.value, None if dup else w, fd=t.fd))
            i += 2
            continue
        assert t.word is not None
        w = t.word
        if not cur.argv and "=" in w.text and _is_assignment(w):
            cur.assigns.append(w)
        elif not cur.argv and w.text in RESERVED and not w.quoted:
            pass  # `then rm x` runs rm
        elif not cur.argv and w.text in LOOP_HEADERS and not w.quoted:
            # `for f in *.csv`: a header, not a command; its words feed a variable
            while i + 1 < len(tokens) and tokens[i + 1].kind == "word":
                i += 1
        elif (
            not cur.argv
            and i + 2 < len(tokens)
            and tokens[i + 1].value == "("
            and (tokens[i + 2].value == ")")
        ):
            opaque.append(f"defines a function {w.text}()")
            i += 2
        else:
            cur.argv.append(w)
            if len(cur.argv) == 1 and w.text in {"function", "eval", "source", "."}:
                opaque.append(f"{w.text} runs code that isn't visible here")
            if len(cur.argv) == 1 and w.text in {"cd", "pushd", "popd"}:
                changes_dir = True
        i += 1
    flush()
    for c in commands:
        if any(w.subst for w in [*c.argv, *c.assigns]) or any(
            r.target is not None and r.target.subst for r in c.redirects
        ):
            opaque.append("command substitution runs a command that isn't visible here")
            break
    for c in commands:
        if c.argv and not c.argv[0].static:
            opaque.append(f"the program name {c.argv[0].raw!r} is only known at run time")
    return ParsedShell(commands, list(dict.fromkeys(opaque)), changes_dir)


def _is_assignment(w: Word) -> bool:
    name = w.raw.split("=", 1)[0]
    return bool(name) and (name[0].isalpha() or name[0] == "_") and name.replace("_", "a").isalnum()
