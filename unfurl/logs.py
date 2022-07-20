import collections.abc
import logging
import logging.config
from enum import IntEnum
import os
import tempfile
import time
from typing import Any, Dict, Tuple, Union
from typing_extensions import NotRequired, TypedDict

import rich
from rich.console import Console


def truncate(s: str, max: int = 1200) -> str:
    if not s:
        return ""
    if len(s) > max:
        return f"{s[:max//2]} [{len(s)} omitted...]  {s[-max//2:]}"
    return s


class Levels(IntEnum):
    CRITICAL = logging.CRITICAL
    ERROR = logging.ERROR
    WARNING = logging.WARNING
    INFO = logging.INFO
    VERBOSE = 15
    DEBUG = logging.DEBUG
    TRACE = 5


LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "file": {"format": "[%(asctime)s] %(name)s:%(levelname)s: %(message)s"}
    },
    "filters": {
        "sensitive": {
            "()": "unfurl.logs.SensitiveFilter",
        }
    },
    "handlers": {
        "console": {
            "class": "unfurl.logs.ColorHandler",
            "level": logging.INFO,
            "filters": ["sensitive"],
        },
        "JobLogHandler": {
            "class": "unfurl.logs.JobLogHandler",
            "level": logging.INFO,
            "filters": ["sensitive"],
        },
    },
    "loggers": {
        "git": {"level": logging.INFO, "handlers": ["console"]},
        "unfurl.job.meta": {
            "handlers": ["JobLogHandler"],
            "level": logging.INFO,
        },
    },
    "root": {"level": Levels.TRACE, "handlers": ["console"]},
}


class UnfurlLogger(logging.Logger):
    def trace(self, msg: str, *args: object, **kwargs: Any) -> None:
        self.log(Levels.TRACE.value, msg, *args, **kwargs)

    def verbose(self, msg: str, *args: object, **kwargs: Any) -> None:
        self.log(Levels.VERBOSE.value, msg, *args, **kwargs)


class JobLogHandler(logging.StreamHandler):
    def emit(self, record: logging.LogRecord) -> None:
        # hide output in terminals
        rich.print(record.msg, end="\x1b[2K\r", flush=True)


class StyleDict(TypedDict):
    bg: NotRequired[Union[str, Tuple[int, int, int]]]
    fg: NotRequired[Union[str, Tuple[int, int, int]]]


class ColorHandler(logging.StreamHandler):
    # https://rich.readthedocs.io/en/stable/markup.html
    RICH_STYLE_LEVEL = {
        Levels.CRITICAL: "black on red",
        Levels.ERROR: "red",
        Levels.WARNING: "yellow",
        Levels.INFO: "cyan",
        Levels.VERBOSE: "grey",
        Levels.DEBUG: "grey",
        Levels.TRACE: "grey",
    }

    def emit(self, record: logging.LogRecord) -> None:
        message = truncate(self.format(record))
        # Hide meta job output because it seems to also be logged captured by
        # the root logger.
        if record.name.startswith("unfurl.job.meta"):
            return

        level = Levels[record.levelname]
        try:
            # Soft wrap prevents rich from breaking lines automatically (needed for emulated terminals, like gitlab CI)
            console = Console(soft_wrap=True, file=self.stream)
            console.print(f"[bold] UNFURL [/bold]", end="")
            console.print(f"[{self.RICH_STYLE_LEVEL[level]}] {level.name} [/]", end="")
            console.print(f" {message}")
        except:
            pass


class sensitive:
    """Base class for marking a value as sensitive. Depending on the context,
    sensitive values will either be encrypted or redacted when outputed.
    """

    redacted_str = "<<REDACTED>>"

    def __sensitive__(self) -> bool:
        return True


class SensitiveFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, collections.abc.Mapping):
            record.args = {
                self.redact(k): self.redact(v) for k, v in record.args.items()  # type: ignore
            }
        else:
            if record.args is not None:
                record.args = tuple(self.redact(a) for a in record.args)
        return True

    @staticmethod
    def redact(value: Union[sensitive, str, object]) -> Union[str, object]:
        return sensitive.redacted_str if isinstance(value, sensitive) else value


def start_collapsible(name: str, section_id: Union[str, int], autoclose) -> bool:
    """Starts a collapsible section in Gitlab CI. Only does it if the
    environment variable CI is set (which is always set in most CI systems).

    Right now, this only does something in CI environments. We could use it to display visual
    indicators for the beginning/end of section in normal terminals.

    Returns:
        bool: True if the collapsible section was started, False otherwise. This is equivalent
        whether or not CI is set
    """
    ci = os.environ.get("CI", False)  # Running in a CI environment (eg GitLab CI)
    if ci:
        # The octal \033 is used instead of \e because python doesn't recognize \e as an escape sequence
        print(
            f"\033[0Ksection_start:{int(time.time())}:task_{section_id}[collapsed={'true' if autoclose else 'false'}]\r\033[0K{name}"
        )
    return ci


def end_collapsible(section_id: Union[str, int]) -> None:
    """Ends a collapsible section in Gitlab CI. See `start_collapsible`."""
    ci = os.environ.get("CI", False)  # Running in a CI environment (eg GitLab CI)
    if ci:
        # The octal \033 is used instead of \e because python doesn't recognize \e as an escape sequence
        print(f"\033[0Ksection_end:{int(time.time())}:task_{section_id}\r\033[0K")
    return ci


def initialize_logging() -> None:
    logging.setLoggerClass(UnfurlLogger)
    logging.captureWarnings(True)
    logging.addLevelName(Levels.TRACE.value, Levels.TRACE.name)
    logging.addLevelName(Levels.VERBOSE.value, Levels.VERBOSE.name)
    if os.getenv("UNFURL_LOGGING"):
        LOGGING["handlers"]["console"]["level"] = Levels[  # type: ignore
            os.getenv("UNFURL_LOGGING").upper()  # type: ignore
        ]
    logging.config.dictConfig(LOGGING)


def set_console_log_level(log_level: int) -> None:
    LOGGING["handlers"]["console"]["level"] = log_level  # type: ignore
    LOGGING["incremental"] = True
    logging.config.dictConfig(LOGGING)


def get_console_log_level() -> Levels:
    return LOGGING["handlers"]["console"]["level"]  # type: ignore


def get_tmplog_path() -> str:
    # mktemp is safe here, we just want a random file path to write too
    return tempfile.mktemp("-unfurl.log", dir=os.environ.get("UNFURL_TMPDIR"))


def add_log_file(filename: str, console_level: Levels = Levels.INFO):
    dir = os.path.dirname(filename)
    if dir and not os.path.isdir(dir):
        os.makedirs(dir)

    handler = logging.FileHandler(filename)
    f = SensitiveFilter()
    formatter = logging.Formatter("[%(asctime)s] %(name)s:%(levelname)s: %(message)s")
    handler.setFormatter(formatter)
    log_level = min(console_level, Levels.DEBUG)
    handler.setLevel(log_level)
    handler.addFilter(f)
    logging.getLogger().addHandler(handler)
    return filename
