# Raw-keystroke console input for troll mode, so the Discord typing indicator
# shows while you type. Falls back to plain input when stdin is not a terminal.

from __future__ import annotations

import contextlib
import logging
import sys
import threading

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import msvcrt
else:
    import termios
    import tty

BACKSPACE = {"\x7f", "\b"}
ENTER = {"\r", "\n"}
CTRL_C = "\x03"
CTRL_D = "\x04"
ESCAPE = "\x1b"
PROMPT = "> "


class Console:
    # Line editor over raw stdin, with a log handler that redraws the prompt

    def __init__(self) -> None:
        self.buffer = ""
        self.lock = threading.Lock()
        self.interactive = sys.stdin.isatty()

    # ------------------------------------------------------------------
    #  Logging integration
    # ------------------------------------------------------------------

    def install_log_handler(self) -> None:
        # Replace console logging so log lines do not eat the typed prompt
        root = logging.getLogger()
        for handler in root.handlers[:]:
            if type(handler) is logging.StreamHandler:
                root.removeHandler(handler)

        handler = _PromptPreservingHandler(self)
        handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        root.addHandler(handler)

    def redraw(self) -> None:
        sys.stdout.write("\r\x1b[2K" + PROMPT + self.buffer)
        sys.stdout.flush()

    # ------------------------------------------------------------------
    #  Reading
    # ------------------------------------------------------------------

    def read_line(self, on_keystroke=None) -> str:
        # Blocks until Enter; on_keystroke fires after each character
        if not self.interactive:
            line = sys.stdin.readline()
            if not line:
                raise EOFError
            return line.rstrip("\n")

        with self.lock:
            self.buffer = ""
        self.redraw()

        with _raw_mode():
            while True:
                char = _read_char()
                if char is None:
                    continue

                if char in ENTER:
                    with self.lock:
                        line, self.buffer = self.buffer, ""
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    return line

                if char == CTRL_C:
                    raise KeyboardInterrupt
                if char == CTRL_D:
                    raise EOFError

                if char in BACKSPACE:
                    with self.lock:
                        if not self.buffer:
                            continue
                        self.buffer = self.buffer[:-1]
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                    continue

                if char < " ":  # other control characters
                    continue

                with self.lock:
                    self.buffer += char
                sys.stdout.write(char)
                sys.stdout.flush()

                if on_keystroke is not None:
                    on_keystroke()


class _PromptPreservingHandler(logging.StreamHandler):
    # Clears the current input line, writes the record, then redraws the prompt

    def __init__(self, console: Console):
        super().__init__()
        self.console = console

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            with self.console.lock:
                buffer = self.console.buffer
            sys.stdout.write("\r\x1b[2K" + message + "\n" + PROMPT + buffer)
            sys.stdout.flush()
        except Exception:
            self.handleError(record)


# ---------------------------------------------------------------------------
#  Platform specifics
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _raw_mode():
    # Put the terminal in cbreak mode for the duration of a line
    if IS_WINDOWS or not sys.stdin.isatty():
        yield
        return

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def _read_char() -> str | None:
    # Read one character, returning None for keys with no textual meaning
    if IS_WINDOWS:
        char = msvcrt.getwch()
        if char in ("\x00", "\xe0"):  # function/arrow key: consume the second byte
            msvcrt.getwch()
            return None
        return char

    char = sys.stdin.read(1)
    if char == ESCAPE:
        # Swallow an ANSI sequence such as an arrow key.
        nxt = sys.stdin.read(1)
        if nxt == "[":
            while True:
                final = sys.stdin.read(1)
                if not final or "@" <= final <= "~":
                    break
        return None
    return char
