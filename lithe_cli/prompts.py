"""All interactive input goes through prompt_toolkit.

One seam for every prompt the CLI shows: the chat line (persistent
history, ANSI-colored prompt), the wizard's questions (inline
validation, hidden API key with star echo), and yes/no confirms.

prompt_toolkit owns the terminal: it draws its own line editor with
proper wide-character widths (no readline multibyte corruption), turns
on and consumes bracketed paste itself, and masks ``is_password``
input with asterisks — the three failure classes this CLI hit when it
used ``input``/``getpass``/readline are all upstream concerns now.
"""

from __future__ import annotations

from prompt_toolkit import prompt
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.validation import ValidationError, Validator

from .ui import RED, YELLOW, ui

_MAX_EMPTY_TRIES = 3

_UTF8_HINT = (
    "\n  这行输入无法按 UTF-8 解码，已丢弃；"
    "请把终端与 locale 设为 UTF-8（如 export LANG=C.UTF-8）后重试。"
)


def history_file():
    """Where chat history persists, next to the saved endpoint."""
    from .config import lithe_home

    return lithe_home() / "history"


def open_history() -> FileHistory:
    """A FileHistory under ``$LITHE_HOME`` (dir created on demand)."""
    path = history_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return InMemoryHistory()
    return FileHistory(str(path))


def chat_line(message: str, history) -> str:
    """The chat REPL prompt: colored, history-searchable, paste-safe."""
    return prompt(
        ANSI(message),
        history=history,
        enable_history_search=True,
    )


class _FnValidator(Validator):
    """Adapt a ``text -> error string | None`` callable for prompt_toolkit."""

    def __init__(self, fn):
        self.fn = fn

    def validate(self, document) -> None:
        problem = self.fn(document.text)
        if problem:
            raise ValidationError(message=problem, cursor_position=len(document.text))


def ask(
    label: str,
    default: str = "",
    password: bool = False,
    validate=None,
) -> str | None:
    """Ask for one value; empty answers keep the default or re-prompt.

    ``validate`` (text -> error string or None) is enforced inline by
    prompt_toolkit — Enter is refused with the message until the value
    passes. Returns None when the user bails out (empty too often,
    Ctrl+C/D).
    """
    message = f"{label} [回车沿用已保存值]: " if password and default else f"{label}: "
    validator = _FnValidator(validate) if validate is not None else None
    for _ in range(_MAX_EMPTY_TRIES):
        try:
            answer = prompt(
                message,
                default=default if not password else "",
                is_password=password,
                validator=validator,
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        except UnicodeDecodeError:
            print(ui.s(_UTF8_HINT, RED))
            continue
        if not answer and default:
            return default
        if not answer:
            print(ui.s("  这个值不能为空。", YELLOW))
            continue
        return answer
    return None


def yes_no(label: str, default: bool) -> bool:
    """A y/n question; empty answer takes the default."""
    hint = "Y/n" if default else "y/N"
    try:
        answer = prompt(f"{label} [{hint}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    except UnicodeDecodeError:
        print(ui.s("\n  这行输入无法按 UTF-8 解码，按默认值处理。", RED))
        return default
    return default if not answer else answer in ("y", "yes", "是")
