import collections.abc
import logging
import logging.config
from enum import IntEnum
import os
import tempfile
from typing import Any, Dict, Tuple, Union
from typing_extensions import NotRequired, TypedDict

import click


def truncate(s: str, max: int=1200) -> str:
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
        }
    },
    "loggers": {
        "git": {"level": logging.INFO, "handlers": ["console"]},
    },
    "root": {"level": Levels.TRACE, "handlers": ["console"]},
}


class UnfurlLogger(logging.Logger):
    def trace(self, msg: str, *args: object, **kwargs: Any) -> None:
        self.log(Levels.TRACE.value, msg, *args, **kwargs)

    def verbose(self, msg: str, *args: object, **kwargs: Any) -> None:
        self.log(Levels.VERBOSE.value, msg, *args, **kwargs)


class StyleDict(TypedDict):
    bg: NotRequired[Union[str, Tuple[int, int, int]]]
    fg: NotRequired[Union[str, Tuple[int, int, int]]]


class ColorHandler(logging.StreamHandler):
    # We can use ANSI colors: https://click.palletsprojects.com/en/8.0.x/api/#click.style
    STYLE_LEVEL: Dict[Levels, StyleDict] = {
        Levels.CRITICAL: {"bg": "bright_red", "fg": "white"},
        Levels.ERROR: {"bg": "red", "fg": "white"},
        Levels.WARNING: {"bg": (255, 126, 0), "fg": "white"},
        Levels.INFO: {"bg": "blue", "fg": "white"},
        Levels.VERBOSE: {"bg": "bright_blue", "fg": "white"},
        Levels.DEBUG: {"bg": "black", "fg": "white"},
        Levels.TRACE: {"bg": "bright_black", "fg": "white"},
    }
    STYLE_MESSAGE: Dict[Levels, StyleDict] = {
        Levels.CRITICAL: {"fg": "bright_red"},
        Levels.ERROR: {"fg": "red"},
        Levels.WARNING: {"fg": (255, 126, 0)},
        Levels.INFO: {"fg": "blue"},
        Levels.VERBOSE: {},
        Levels.DEBUG: {},
        Levels.TRACE: {},
    }

    def emit(self, record: logging.LogRecord) -> None:
        message = truncate(self.format(record))
        level = Levels[record.levelname]
        try:
            click.secho(
                " UNFURL ", nl=False, file=self.stream, fg="white", bg="bright_cyan"
            )
            click.secho(
                f" {level.name} ", nl=False, file=self.stream, **self.STYLE_LEVEL[level]
            )
            click.secho(f" {message}", file=self.stream, **self.STYLE_MESSAGE[level])
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
