import collections.abc
import json
import logging
import logging.config
from enum import IntEnum
import sys
import os
import re
import tempfile
import time
import types
from typing import Any, Union, Set, List, Optional, cast

import rich
from rich.console import Console

try:
    from ansible.parsing.yaml.objects import AnsibleVaultEncryptedUnicode
except ImportError:
    AnsibleVaultEncryptedUnicode = None

HIDDEN_MSG_LOGGER = "unfurl.metadata"

DEFAULT_TRUNCATE_LENGTH = int(os.getenv("UNFURL_LOG_TRUNCATE") or 748)

sensitive_params = (
    "token",
    "secret",
    "password",
)  # note: matches private_token etc too
sensitive_params_regex = re.compile(
    rf"({'|'.join(sensitive_params)})=([^&\s]+)", re.IGNORECASE
)
sensitive_args_regex = re.compile(
    rf"""(--\S{{0,50}}({"|".join(sensitive_params)})('|")?(=|\s+))(\S+)""",
    re.IGNORECASE,
)


def sensitive_keyname(k) -> bool:
    if isinstance(k, str):
        return any(param in k.lower() for param in sensitive_params + ("key",))
    return False


def truncate(s: str, max: int = DEFAULT_TRUNCATE_LENGTH, omitted="omitted...") -> str:
    if not s:
        return ""
    if len(s) > max:
        return f"{s[: max // 2]} [{len(s)} {omitted}]  {s[-max // 2 :]}"
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
        },
        "log_once": {
            "()": "unfurl.logs.LogOnceFilter",
        },
    },
    "handlers": {
        "console": {
            "class": "unfurl.logs.ColorHandler",
            "level": logging.INFO,
            "filters": ["log_once"],
        },
        "HiddenOutputLogHandler": {
            "class": "unfurl.logs.HiddenOutputLogHandler",
            "level": logging.INFO,
            "filters": ["log_once"],
        },
    },
    "loggers": {
        "blib2to3.pgen2.driver": {"level": logging.INFO, "handlers": ["console"]},
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


PY_COLORS = os.environ.get("PY_COLORS") != "0"


def getConsole(**kwargs) -> Console:
    global PY_COLORS
    PY_COLORS = os.environ.get("PY_COLORS") != "0"
    # Settings needed for emulated terminals, like gitlab CI:
    # - Soft wrap prevents rich from breaking lines automatically
    # - force_terminal to display colors
    return Console(soft_wrap=True, force_terminal=PY_COLORS, **kwargs)


class sensitive:
    """Base class for marking a value as sensitive. Depending on the context,
    sensitive values will either be encrypted or redacted when outputed.
    """

    redacted_str = "<<REDACTED>>"
    redacted_bytes = b"<<REDACTED>>"

    def __sensitive__(self) -> bool:
        return True


def is_sensitive(obj: object) -> bool:
    test = getattr(obj, "__sensitive__", None)
    if isinstance(test, types.MethodType):
        return test()
    if AnsibleVaultEncryptedUnicode and isinstance(obj, AnsibleVaultEncryptedUnicode):
        return True
    elif isinstance(obj, collections.abc.Mapping):
        return any(is_sensitive(i) for i in obj.values())
    elif isinstance(obj, collections.abc.MutableSequence):
        return any(is_sensitive(i) for i in obj)
    return False


class LogOnceFilter(logging.Filter):
    """Filter that prevents duplicate log messages from being logged.

    Tracks messages by their message string and arguments. When a log record
    has extra={'log_once': True}, it will only be logged the first time that
    combination of message and args is seen.
    """

    def __init__(self, name: str = ""):
        super().__init__(name)
        self._logged_messages: Set[tuple] = set()

    def filter(self, record: logging.LogRecord) -> bool:
        """Filter out messages that have already been logged.

        Args:
            record: The log record to filter

        Returns:
            True if the message should be logged, False otherwise
        """
        # Only filter if log_once is explicitly set to True
        if not getattr(record, "log_once", False):
            return True

        # Create a hashable key from the message and args
        msg_key = self._make_key(record.msg, record.args)

        # Check if we've seen this message before
        if msg_key in self._logged_messages:
            return False

        # First time seeing this message, add it to the set
        self._logged_messages.add(msg_key)
        return True

    @staticmethod
    def _make_key(msg: object, args: object) -> tuple:
        """Create a hashable key from a message and its arguments.

        Args:
            msg: The log message
            args: The log message arguments

        Returns:
            A hashable tuple that uniquely identifies this message
        """
        # Convert msg to string representation
        msg_str = str(msg)

        # Convert args to a hashable format
        args_tuple: tuple
        if args is None:
            args_tuple = ()
        elif isinstance(args, tuple):
            # Try to make each arg hashable
            args_tuple = tuple(
                arg
                if isinstance(arg, (str, int, float, bool, type(None)))
                else str(arg)
                for arg in args
            )
        elif isinstance(args, dict):
            # Convert dict to sorted tuple of items
            args_tuple = tuple(
                sorted(
                    (
                        k,
                        v
                        if isinstance(v, (str, int, float, bool, type(None)))
                        else str(v),
                    )
                    for k, v in args.items()
                )
            )
        else:
            args_tuple = (str(args),)

        return (msg_str, args_tuple)


class SensitiveFilter(logging.Filter):
    # If supporting Python <3.12, consider using SensitiveStreamHandler instead to avoid redaction bleeding across handlers
    @staticmethod
    def filter(record: logging.LogRecord):  # type: ignore[override]
        if sys.version_info >= (3, 12, 0):
            # starting in 3.12, we can return a record instead of modifying it in-place,
            # allowing other handlers to receive the original record
            record = logging.makeLogRecord(record.__dict__)

        # Sanitize the message and collect indices of format specifiers that need redaction
        indices_to_redact: List[int] = []
        if isinstance(record.msg, str):
            record.msg = SensitiveFilter.sanitize_urls(record.msg, indices_to_redact)

        # Redact arguments based on collected indices or apply normal redaction
        if record.args is not None:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    "XXXXX" if idx in indices_to_redact else SensitiveFilter.redact(a)
                    for idx, a in enumerate(record.args)
                )
            else:
                record.args = SensitiveFilter.redact(record.args)  # type: ignore

        return record

    @staticmethod
    def sanitize_urls(value: str, format_spec_indices: Optional[List[int]] = None) -> str:
        value = re.sub(r"//(\S{1,100}):(\S{1,200})@", r"//\1:XXXXX@", value)

        if format_spec_indices is not None:
            # Combined regex that matches EITHER sensitive params OR bare format specifiers
            # This processes left-to-right and counts all format specs
            combined_pattern = rf"({'|'.join(sensitive_params)})=([^&\s]+)|(%[sdifr])"

            spec_count = 0

            def replace_match(match):
                nonlocal spec_count
                if match.group(3):  # group 3 is the bare format specifier
                    # Just a format spec, count it
                    spec_count += 1
                    return match.group(0)  # Don't modify
                else:
                    # It's a sensitive param (groups 1 and 2)
                    param_name = match.group(1)
                    param_value = match.group(2)
                    if param_value.startswith('%'):
                        # It's a sensitive param with a format specifier
                        format_spec_indices.append(spec_count)
                        spec_count += 1
                        return match.group(0)  # Don't modify the format spec
                    else:
                        # Regular sensitive value, redact it
                        return f"{param_name}=XXXXX"

            return re.sub(combined_pattern, replace_match, value)
        else:
            # When not tracking indices, use original behavior
            def replace_sensitive_param(match):
                param_name = match.group(1)
                param_value = match.group(2)
                if param_value.startswith('%'):
                    return match.group(0)
                return f"{param_name}=XXXXX"
            return re.sub(sensitive_params_regex, replace_sensitive_param, value)

    @staticmethod
    def sanitize_str(value: str) -> str:
        "Look for suspicious urls and command args and redact them."
        value = SensitiveFilter.sanitize_urls(value)
        def replace_sensitive_arg(match):
            prefix = match.group(1)
            arg_value = match.group(5)
            # Don't replace format specifiers
            if arg_value.startswith('%'):
                return match.group(0)
            return f"{prefix}XXXXX"
        return re.sub(sensitive_args_regex, replace_sensitive_arg, value)

    @staticmethod
    def redact(value: Union[sensitive, str, object]) -> Union[str, object]:
        if isinstance(value, collections.abc.Mapping):
            if isinstance(value, sensitive):
                # e.g. sensitive_dict — show keys but redact values
                return f"<<REDACTED keys: {', '.join(map(str, value))}>>"
            return {
                SensitiveFilter.redact(k): sensitive.redacted_str
                if sensitive_keyname(k)
                else SensitiveFilter.redact(v)
                for k, v in value.items()
            }
        elif is_sensitive(value):
            return sensitive.redacted_str
        elif isinstance(value, str):
            # make sure urls with credentials don't leak
            return SensitiveFilter.sanitize_str(value)
        else:
            return value


class SensitiveStreamHandler(logging.StreamHandler):
    """
    A StreamHandler that runs SensitiveFilter on a per-handler clone of the
    LogRecord so its redactions don't bleed into other handlers sharing
    the same record. Required on Python <3.12 where Filterer.filter()
    ignores any LogRecord returned by a filter, so SensitiveFilter's
    in-place mutation of the shared record would leak to downstream
    handlers.

    Pass ``sensitive=False`` (e.g. via the LOGGING dict) to disable
    redaction for a specific handler.
    """

    def __init__(self, *args, sensitive: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        self.sensitive = sensitive

    def handle(self, record: logging.LogRecord):
        if not self.sensitive:
            return super().handle(record)
        if sys.version_info < (3, 12, 0):
            # pre-3.12 filters mutate in-place; clone so we don't leak to
            # other handlers sharing the record. On 3.12+ SensitiveFilter.filter
            # does its own clone internally and returns it.
            record = logging.makeLogRecord(record.__dict__)
        record = SensitiveFilter.filter(record)
        return super().handle(record)


class HiddenOutputLogHandler(SensitiveStreamHandler):
    def emit(self, record: logging.LogRecord) -> None:
        # hide output in terminals
        rich.print(record.msg, end="\x1b[2K\r", flush=True)


class ColorHandler(SensitiveStreamHandler):
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
        truncate_length = getattr(record, "truncate", DEFAULT_TRUNCATE_LENGTH)
        # don't truncate stack traces
        if truncate_length and not record.exc_info and record.stack_info:
            if record.exc_text:
                truncate_length = max(len(record.exc_text) * 2, truncate_length)
            if record.stack_info:
                truncate_length = max(len(record.stack_info) * 2, truncate_length)
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
                console.out(
                    json.dumps(data), end="\x1b[2K\r", highlight=False, style=None
                )

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
    # Apply incremental mode via a shallow copy — don't mutate the module-
    # level LOGGING dict, since a permanent `incremental: True` there would
    # break any later non-incremental dictConfig(LOGGING) call (it would
    # fail to (re-)create handlers).
    cfg = dict(LOGGING)
    cfg["incremental"] = True
    logging.config.dictConfig(cfg)


def get_console_log_level() -> Levels:
    return Levels(LOGGING["handlers"]["console"]["level"])  # type: ignore


def get_tmplog_path() -> str:
    # mktemp is safe here, we just want a random file path to write too
    return tempfile.mktemp("-unfurl.log", dir=os.environ.get("UNFURL_TMPDIR"))


def add_log_file(filename: str, console_level: Levels = Levels.INFO):
    dir = os.path.dirname(filename)
    if dir and not os.path.isdir(dir):
        os.makedirs(dir)

    handler = logging.FileHandler(filename)
    fmt = (
        os.getenv("UNFURL_LOG_FORMAT")
        or "[%(asctime)s] %(name)s:%(levelname)s: %(message)s"
    )
    formatter = logging.Formatter(fmt)
    handler.setFormatter(formatter)
    log_level = min(console_level, Levels.DEBUG)
    handler.setLevel(log_level)
    handler.addFilter(LogOnceFilter())
    logging.getLogger().addHandler(handler)
    return filename
