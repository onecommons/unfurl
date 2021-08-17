import collections.abc
import logging
import logging.config
from enum import Enum
import os

import click

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "sensitive": {
            "()": "unfurl.logs.SensitiveFilter",
        }
    },
    "handlers": {
        "console": {"class": "unfurl.logs.ColorHandler", "filters": ["sensitive"]}
    },
    "loggers": {
        "git": {"level": "INFO", "handlers": ["console"]},
    },
    "root": {"level": "INFO", "handlers": ["console"]},
}


class Levels(Enum):
    CRITICAL = logging.CRITICAL
    ERROR = logging.ERROR
    WARNING = logging.WARNING
    INFO = logging.INFO
    VERBOSE = 15
    DEBUG = logging.DEBUG
    TRACE = 5


class UnfurlLogger(logging.Logger):
    def trace(self, msg, *args, **kwargs):
        self.log(Levels.TRACE.value, msg, *args, **kwargs)

    def verbose(self, msg, *args, **kwargs):
        self.log(Levels.VERBOSE.value, msg, *args, **kwargs)


class ColorHandler(logging.StreamHandler):
    # We can use ANSI colors: https://click.palletsprojects.com/en/8.0.x/api/#click.style
    STYLE_LEVEL = {
        Levels.CRITICAL: {"bg": "bright_red", "fg": "white"},
        Levels.ERROR: {"bg": "red", "fg": "white"},
        Levels.WARNING: {"bg": (255, 126, 0), "fg": "white"},
        Levels.INFO: {"bg": "blue", "fg": "white"},
        Levels.VERBOSE: {"bg": "bright_blue", "fg": "white"},
        Levels.DEBUG: {"bg": "black", "fg": "white"},
        Levels.TRACE: {"bg": "bright_black", "fg": "white"},
    }
    STYLE_MESSAGE = {
        Levels.CRITICAL: {"fg": "bright_red"},
        Levels.ERROR: {"fg": "red"},
        Levels.WARNING: {"fg": (255, 126, 0)},
        Levels.INFO: {"fg": "blue"},
        Levels.VERBOSE: {},
        Levels.DEBUG: {},
        Levels.TRACE: {},
    }

    def emit(self, record: logging.LogRecord) -> None:
        message = self.format(record)
        level = Levels[record.levelname]
        click.secho(
            " UNFURL ", nl=False, file=self.stream, fg="white", bg="bright_cyan"
        )
        click.secho(
            f" {level.name} ", nl=False, file=self.stream, **self.STYLE_LEVEL[level]
        )
        click.secho(f" {message}", file=self.stream, **self.STYLE_MESSAGE[level])


class sensitive:
    """Base class for marking a value as sensitive. Depending on the context,
    sensitive values will either be encrypted or redacted when outputed.
    """

    redacted_str = "<<REDACTED>>"

    def __sensitive__(self):
        return True


class SensitiveFilter(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, collections.abc.Mapping):
            record.args = {
                self.redact(k): self.redact(v) for k, v in record.args.items()
            }
        else:
            record.args = tuple(self.redact(a) for a in record.args)
        return True

    @staticmethod
    def redact(value):
        return sensitive.redacted_str if isinstance(value, sensitive) else value


def initialize_logging():
    logging.setLoggerClass(UnfurlLogger)
    logging.captureWarnings(True)
    logging.addLevelName(Levels.TRACE.value, Levels.TRACE.name)
    logging.addLevelName(Levels.VERBOSE.value, Levels.VERBOSE.name)
    logging.config.dictConfig(LOGGING)
    if os.getenv("UNFURL_LOGGING"):
        effective_log_level = Levels[os.getenv("UNFURL_LOGGING").upper()]
        set_root_log_level(effective_log_level.value)


def set_root_log_level(log_level: int):
    logging.getLogger().setLevel(log_level)


def add_log_file(filename):
    handler = logging.FileHandler(filename)
    f = SensitiveFilter()
    formatter = logging.Formatter("[%(asctime)s] %(name)s:%(levelname)s: %(message)s")
    handler.setFormatter(formatter)
    handler.setLevel(Levels.TRACE.value)
    handler.addFilter(f)
    logging.getLogger().addHandler(handler)
