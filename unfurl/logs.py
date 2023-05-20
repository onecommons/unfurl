import collections.abc
import json
import logging
import logging.config
from enum import IntEnum
import os
import tempfile
import time
import types
from typing import Any, Union, cast

import rich
from rich.console import Console

try:
    from ansible.parsing.yaml.objects import AnsibleVaultEncryptedUnicode
except ImportError:
    AnsibleVaultEncryptedUnicode = None

HIDDEN_MSG_LOGGER = "unfurl.metadata"

DEFAULT_TRUNCATE_LENGTH = 748


def truncate(s: str, max: int = DEFAULT_TRUNCATE_LENGTH) -> str:
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
        "HiddenOutputLogHandler": {
            "class": "unfurl.logs.HiddenOutputLogHandler",
            "level": logging.INFO,
            "filters": ["sensitive"],
        },
    },
    "loggers": {
        "git": {"level": logging.INFO, "handlers": ["console"]},
        HIDDEN_MSG_LOGGER: {
            "handlers": ["HiddenOutputLogHandler"],
            "level": logging.INFO,
        },
    },
    "root": {"level": Levels.TRACE, "handlers": ["console"]},
}


class LogExtraLevels:
    def trace(self, msg: str, *args: object, **kwargs: Any) -> None:
        self.log(Levels.TRACE.value, msg, *args, **kwargs)  # type: ignore

    def verbose(self, msg: str, *args: object, **kwargs: Any) -> None:
        self.log(Levels.VERBOSE.value, msg, *args, **kwargs)  # type: ignore


class UnfurlLogger(logging.Logger, LogExtraLevels):
    pass


def getLogger(name: str) -> UnfurlLogger:
    return cast(UnfurlLogger, logging.getLogger(name))


class HiddenOutputLogHandler(logging.StreamHandler):
    def emit(self, record: logging.LogRecord) -> None:
        # hide output in terminals
        rich.print(record.msg, end="\x1b[2K\r", flush=True)


PY_COLORS = os.environ.get("PY_COLORS") != "0"


def getConsole(**kwargs) -> Console:
    global PY_COLORS
    PY_COLORS = os.environ.get("PY_COLORS") != "0"
    # Settings needed for emulated terminals, like gitlab CI:
    # - Soft wrap prevents rich from breaking lines automatically
    # - force_terminal to display colors
    return Console(soft_wrap=True, force_terminal=PY_COLORS, **kwargs)


class ColorHandler(logging.StreamHandler):
    # https://rich.readthedocs.io/en/stable/appendix/colors.html
    RICH_STYLE_LEVEL = {
        Levels.CRITICAL: "white on bright_red",
        Levels.ERROR: "white on red",
        Levels.WARNING: "white on dark_orange",  # #ff8700
        Levels.INFO: "white on blue",
        Levels.VERBOSE: "white on bright_blue",
        Levels.DEBUG: "white on black",
        Levels.TRACE: "white on bright_black",
    }

    def emit(self, record: logging.LogRecord) -> None:
        message = self.format(record)
        if not record.exc_info and not record.stack_info:
            truncate_length = getattr(record, "truncate", DEFAULT_TRUNCATE_LENGTH)
            if truncate_length:
                message = truncate(message, truncate_length)
        # Hide meta job output because it seems to also be logged captured by
        # the root logger.
        if record.name.startswith(HIDDEN_MSG_LOGGER):
            return

        level = Levels[record.levelname]
        try:
            console = getConsole(file=self.stream)
            data = getattr(record, "json", None)
            if data and os.environ.get("CI", False):
                # Running in a CI environment (eg GitLab CI)
                console.out(json.dumps(data), end="\x1b[2K\r", highlight=False, style=None)

            console.print(
                f"[{self.RICH_STYLE_LEVEL[level]}] {level.name.center(8)}[/]", end=""
            )
            kw = dict(markup=False)
            if hasattr(record, "rich"):
                kw.update(record.rich)  # type: ignore
            console.print(f" {record.name.upper()}", end="")
            console.print(f" {message}", **kw)  # type: ignore
        except Exception:
            if os.environ.get("UNFURL_RAISE_LOGGING_EXCEPTIONS"):
                raise
            self.stream.write(f"Log error: exception while logging {message}")


class sensitive:
    """Base class for marking a value as sensitive. Depending on the context,
    sensitive values will either be encrypted or redacted when outputed.
    """

    redacted_str = "<<REDACTED>>"

    def __sensitive__(self) -> bool:
        return True


def is_sensitive(obj: object) -> bool:
    test = getattr(obj, "__sensitive__", None)
    if test and isinstance(test, types.MethodType):
        return test()
    if AnsibleVaultEncryptedUnicode and isinstance(obj, AnsibleVaultEncryptedUnicode):
        return True
    elif isinstance(obj, collections.abc.Mapping):
        return any(is_sensitive(i) for i in obj.values())
    elif isinstance(obj, collections.abc.MutableSequence):
        return any(is_sensitive(i) for i in obj)
    return False


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
        return sensitive.redacted_str if is_sensitive(value) else value


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
    return bool(ci)


def end_collapsible(section_id: Union[str, int]) -> bool:
    """Ends a collapsible section in Gitlab CI. See `start_collapsible`."""
    ci = os.environ.get("CI", False)  # Running in a CI environment (eg GitLab CI)
    if ci:
        # The octal \033 is used instead of \e because python doesn't recognize \e as an escape sequence
        print(f"\033[0Ksection_end:{int(time.time())}:task_{section_id}\r\033[0K")
    return bool(ci)


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
    if os.getenv("UNFURL_LOG_FORMAT"):
        formatter = logging.Formatter(os.getenv("UNFURL_LOG_FORMAT"))
        logging.getLogger().handlers[0].setFormatter(formatter)


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
    fmt = (
        os.getenv("UNFURL_LOG_FORMAT")
        or "[%(asctime)s] %(name)s:%(levelname)s: %(message)s"
    )
    formatter = logging.Formatter(fmt)
    handler.setFormatter(formatter)
    log_level = min(console_level, Levels.DEBUG)
    handler.setLevel(log_level)
    handler.addFilter(f)
    logging.getLogger().addHandler(handler)
    return filename
