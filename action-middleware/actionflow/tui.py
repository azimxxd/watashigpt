"""Styled terminal output (ANSI colours, boxes, arrow-key selector).

Thread-safe: every print goes through one lock.
"""

from __future__ import annotations

import select
import shutil
import sys
import termios
import threading
import tty
from datetime import datetime
from actionflow.privacy import private_output, log_texts



class TUI:
    @classmethod
    def disable_colors(cls) -> None:
        """Plain output for log files (no terminal attached)."""
        for name, value in list(vars(cls).items()):
            if isinstance(value, str) and value.startswith("\033["):
                setattr(cls, name, "")

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    ITALIC = "\033[3m"

    BLACK = "\033[30m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"

    BG_BLACK = "\033[40m"
    BG_RED = "\033[41m"
    BG_GREEN = "\033[42m"
    BG_YELLOW = "\033[43m"
    BG_BLUE = "\033[44m"
    BG_MAGENTA = "\033[45m"
    BG_CYAN = "\033[46m"
    BG_WHITE = "\033[47m"

    TOP_LEFT = "╭"
    TOP_RIGHT = "╮"
    BOT_LEFT = "╰"
    BOT_RIGHT = "╯"
    HORIZ = "─"
    VERT = "│"

    _print_lock = threading.Lock()

    @staticmethod
    def _width() -> int:
        return shutil.get_terminal_size((60, 20)).columns

    @classmethod
    def _strip_ansi(cls, text: str) -> str:
        import re
        return re.sub(r"\033\[[0-9;]*m", "", text)

    @classmethod
    def _timestamp(cls) -> str:
        return f"{cls.DIM}{datetime.now().strftime('%H:%M:%S')}{cls.RESET}"

    @classmethod
    def _read_key(cls) -> str:
        """Read a single keypress in raw mode. Returns 'left', 'right', 'enter', etc."""
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == '\x1b':
                if select.select([sys.stdin], [], [], 0.1)[0]:
                    ch2 = sys.stdin.read(1)
                    if ch2 == '[':
                        ch3 = sys.stdin.read(1)
                        if ch3 == 'D':
                            return 'left'
                        elif ch3 == 'C':
                            return 'right'
                        elif ch3 == 'A':
                            return 'up'
                        elif ch3 == 'B':
                            return 'down'
                return 'escape'
            elif ch in ('\r', '\n'):
                return 'enter'
            elif ch == '\x03':
                return 'ctrl_c'
            else:
                return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    @classmethod
    def selector(cls, options: list[str]) -> int | None:
        """Arrow-key horizontal selector. Returns index or None if cancelled."""
        current = 0
        # The hint goes on its own line: if the option row wrapped, "\r" could
        # only redraw its last line and the row would be duplicated.
        sys.stdout.write(f"  {cls.DIM}← → to move, enter to select{cls.RESET}\n")

        while True:
            parts = []
            for i, opt in enumerate(options):
                if i == current:
                    parts.append(f"{cls.BG_CYAN}{cls.BLACK}{cls.BOLD} {opt} {cls.RESET}")
                else:
                    parts.append(f"{cls.DIM} {opt} {cls.RESET}")
            line = " ".join(parts)
            sys.stdout.write(f"\r  {line}\033[K")
            sys.stdout.flush()

            key = cls._read_key()
            if key == 'left':
                current = (current - 1) % len(options)
            elif key == 'right':
                current = (current + 1) % len(options)
            elif key == 'enter':
                parts = []
                for i, opt in enumerate(options):
                    if i == current:
                        parts.append(f"{cls.GREEN}{cls.BOLD} {opt} {cls.RESET}")
                    else:
                        parts.append(f"{cls.DIM} {opt} {cls.RESET}")
                sys.stdout.write(f"\r  {'  '.join(parts)}\033[K\n")
                sys.stdout.flush()
                return current
            elif key in ('ctrl_c', 'escape'):
                sys.stdout.write("\r\033[K\n")
                sys.stdout.flush()
                return None

    @classmethod
    def selector_vertical(cls, options: list[str]) -> int | None:
        """Arrow-key list (↑/↓ or number, enter). Returns index or None."""
        current = 0
        n = len(options)
        sys.stdout.write(f"  {cls.DIM}↑ ↓ to move · enter to select{cls.RESET}\n")

        def draw(first: bool) -> None:
            if not first:
                sys.stdout.write(f"\033[{n}A")
            for i, opt in enumerate(options):
                if i == current:
                    line = f"  {cls.CYAN}{cls.BOLD}❯ {opt}{cls.RESET}"
                else:
                    line = f"    {cls.DIM}{opt}{cls.RESET}"
                sys.stdout.write(f"\r{line}\033[K\n")
            sys.stdout.flush()

        draw(True)
        while True:
            key = cls._read_key()
            if key in ('up', 'left'):
                current = (current - 1) % n
            elif key in ('down', 'right'):
                current = (current + 1) % n
            elif key.isdigit() and 1 <= int(key) <= n:
                current = int(key) - 1
            elif key == 'enter':
                return current
            elif key in ('ctrl_c', 'escape'):
                return None
            draw(False)

    @classmethod
    def _print(cls, *args, **kwargs) -> None:
        if private_output.get():
            return
        with cls._print_lock:
            print(*args, **kwargs)
            sys.stdout.flush()

    @classmethod
    def box(cls, title: str, lines: list[str], color: str = "") -> None:
        c = color or cls.CYAN
        w = cls._width() - 2
        inner = w - 2

        title_text = f" {title} "
        pad = inner - len(title_text)
        left_pad = pad // 2
        right_pad = pad - left_pad

        with cls._print_lock:
            print(f"{c}{cls.TOP_LEFT}{cls.HORIZ * left_pad}{cls.BOLD}{title_text}{cls.RESET}{c}{cls.HORIZ * right_pad}{cls.TOP_RIGHT}{cls.RESET}")
            for line in lines:
                visible_len = len(cls._strip_ansi(line))
                spacing = max(0, inner - visible_len)
                print(f"{c}{cls.VERT}{cls.RESET} {line}{' ' * spacing}{c}{cls.VERT}{cls.RESET}")
            print(f"{c}{cls.BOT_LEFT}{cls.HORIZ * inner}{cls.HORIZ * 2}{cls.BOT_RIGHT}{cls.RESET}")
            sys.stdout.flush()

    @classmethod
    def banner(cls) -> None:
        w = cls._width() - 2
        inner = w - 2
        c = cls.MAGENTA

        logo = [
            "  ▄▀█ █▀▀ ▀█▀ █ █▀█ █▄░█",
            "  █▀█ █▄▄ ░█░ █ █▄█ █░▀█",
            "",
            "  █▀▀ █░░ █▀█ █░█░█",
            "  █▀░ █▄▄ █▄█ ▀▄▀▄▀",
        ]

        with cls._print_lock:
            print(f"\n{c}{cls.TOP_LEFT}{cls.HORIZ * inner}{cls.HORIZ * 2}{cls.TOP_RIGHT}{cls.RESET}")
            for line in logo:
                visible_len = len(line)
                spacing = max(0, inner - visible_len)
                print(f"{c}{cls.VERT}{cls.RESET} {cls.BOLD}{cls.MAGENTA}{line}{cls.RESET}{' ' * spacing}{c}{cls.VERT}{cls.RESET}")
            print(f"{c}{cls.BOT_LEFT}{cls.HORIZ * inner}{cls.HORIZ * 2}{cls.BOT_RIGHT}{cls.RESET}\n")
            sys.stdout.flush()

    @classmethod
    def status(cls, label: str, value: str, color: str = "") -> None:
        c = color or cls.WHITE
        cls._print(f"  {cls._timestamp()}  {c}{cls.BOLD}{label}{cls.RESET} {cls.DIM}{value}{cls.RESET}")

    @classmethod
    def success(cls, message: str) -> None:
        cls._print(f"  {cls._timestamp()}  {cls.GREEN}✓{cls.RESET} {message}")

    @classmethod
    def warn(cls, message: str) -> None:
        cls._print(f"  {cls._timestamp()}  {cls.YELLOW}⚠{cls.RESET} {message}")

    @classmethod
    def error(cls, message: str) -> None:
        cls._print(f"  {cls._timestamp()}  {cls.RED}✗{cls.RESET} {message}")

    @classmethod
    def action(cls, icon: str, label: str, detail: str) -> None:
        cls._print(f"  {cls._timestamp()}  {cls.CYAN}{icon}{cls.RESET} {cls.BOLD}{label}{cls.RESET} {cls.DIM}→{cls.RESET} {detail}")

    @classmethod
    def separator(cls) -> None:
        w = cls._width() - 4
        cls._print(f"  {cls.DIM}{cls.HORIZ * w}{cls.RESET}")

    @classmethod
    def activity_entry(cls, cmd_name: str, input_text: str, output_text: str,
                       duration: float, is_llm: bool = False, is_error: bool = False,
                       trigger: str = "") -> None:
        """Print a single activity feed line."""
        ts = datetime.now().strftime('%H:%M:%S')

        if is_error:
            color = cls.RED
        elif is_llm:
            color = cls.MAGENTA
        else:
            color = cls.CYAN

        input_text, output_text = log_texts(cmd_name, input_text, output_text)
        max_len: int = max(20, (cls._width() - 50) // 2)
        inp = input_text[:max_len] + ("..." if len(input_text) > max_len else "")
        out = output_text[:max_len] + ("..." if len(output_text) > max_len else "")

        check = f"{cls.GREEN}✓{cls.RESET}" if not is_error else f"{cls.RED}✗{cls.RESET}"
        trigger_tag = f"  {cls.DIM}[{trigger}]{cls.RESET}" if trigger else ""

        line = (
            f"  {cls.DIM}{ts}{cls.RESET}  "
            f"{color}{cls.BOLD}{cmd_name.upper():<10}{cls.RESET}  "
            f"{cls.DIM}\"{inp}\" → \"{out}\"{cls.RESET}   "
            f"{check} {cls.DIM}{duration:.1f}s{cls.RESET}{trigger_tag}"
        )
        token = private_output.set(False)
        try:
            cls._print(line)
        finally:
            private_output.reset(token)

    @classmethod
    def micro_log(cls, message: str) -> None:
        """Print a dim, timestamped status line."""
        ts = datetime.now().strftime('%H:%M:%S')
        cls._print(f"  {cls.DIM}{ts}{cls.RESET}  {message}")
