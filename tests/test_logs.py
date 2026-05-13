import logging
import os
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

@pytest.fixture
def caplog_with_filters(caplog):
    """Caplog fixture that adds Unfurl's filters to the caplog handler."""
    # Add LogOnceFilter to caplog's handler so it captures filtered behavior
    log_once_filter = LogOnceFilter()
    caplog.handler.addFilter(log_once_filter)
    yield caplog
    # Cleanup
    caplog.handler.removeFilter(log_once_filter)


from unfurl.__main__ import detect_log_level, detect_verbose_level
from unfurl.job import JobOptions, Runner
from unfurl.localenv import LocalEnv
from unfurl.logs import Levels, SensitiveFilter, LogOnceFilter, getConsole, getLogger
from unfurl.util import sensitive_str
from unfurl import logs
from ruamel.yaml.comments import CommentedMap


def test_format_of_job_file_log():
    tmplogfile = logs.get_tmplog_path()
    logs.add_log_file(tmplogfile)
    logger = logs.getLogger("unfurl")
    logger.info("starting deploy job for ")

    with open(tmplogfile) as f:
        first_line = f.readline()
        # check format - it should differ from console log
        expected_format = r"\[.+\] unfurl:INFO: starting deploy job for .*"
        assert re.match(expected_format, first_line)
    os.unlink(tmplogfile)


class TestLogLevelDetection:
    def setup_method(self):
        self.old_env_level = os.environ.get("UNFURL_LOGGING")
        if self.old_env_level:
            del os.environ["UNFURL_LOGGING"]

    def teardown_method(self):
        if self.old_env_level:
            os.environ["UNFURL_LOGGING"] = self.old_env_level

    def test_default(self):
        level = detect_log_level(loglevel=None, quiet=False, verbose=0)
        assert level is Levels.INFO

    def test_quiet_mode(self):
        level = detect_log_level(loglevel=None, quiet=True, verbose=0)
        assert level is Levels.CRITICAL

    def test_from_env_var(self):
        os.environ["UNFURL_LOGGING"] = "DEBUG"
        level = detect_log_level(loglevel=None, quiet=False, verbose=0)
        assert level is Levels.DEBUG

    @pytest.mark.parametrize(
        ["log_level", "expected"],
        [
            ("CRITICAL", Levels.CRITICAL),
            ("ERROR", Levels.ERROR),
            ("WARNING", Levels.WARNING),
            ("INFO", Levels.INFO),
            ("VERBOSE", Levels.VERBOSE),
            ("DEBUG", Levels.DEBUG),
            ("TRACE", Levels.TRACE),
        ],
    )
    def test_based_on_provided_log_level(self, log_level, expected):
        level = detect_log_level(loglevel=log_level, quiet=False, verbose=0)
        assert level is expected

    @pytest.mark.parametrize(
        ["verbose", "expected"],
        [
            (0, Levels.INFO),
            (1, Levels.VERBOSE),
            (2, Levels.DEBUG),
            (3, Levels.TRACE),
        ],
    )
    def test_log_level_based_on_verbose(self, verbose, expected):
        level = detect_log_level(loglevel=None, quiet=False, verbose=verbose)
        assert level is expected


class TestVerboseLevelDetection:
    @pytest.mark.parametrize(
        ["log_level", "expected"],
        [
            (Levels.CRITICAL, -1),
            (Levels.ERROR, 0),
            (Levels.WARNING, 0),
            (Levels.INFO, 0),
            (Levels.VERBOSE, 1),
            (Levels.DEBUG, 2),
            (Levels.TRACE, 3),
        ],
    )
    def test_default(self, log_level, expected):
        verbose = detect_verbose_level(log_level)
        assert verbose == expected


class TestSensitiveFilter:
    not_important_args = {
        "name": "xxx",
        "level": logging.ERROR,
        "pathname": "yyy",
        "lineno": 1,
        "exc_info": None,
    }

    def setup_method(self):
        self.sensitive_filter = SensitiveFilter()

    def test_tuple_log_record(self):
        record = logging.LogRecord(
            msg="This should %s, %s",
            args=("work", sensitive_str("not this")),
            **self.not_important_args,
        )

        record = self.sensitive_filter.filter(record)

        assert record.getMessage() == "This should work, <<REDACTED>>"

    def test_dict_log_record(self):
        record = logging.LogRecord(
            msg="This should %s",
            args=({"also": "work", "not": sensitive_str("not this")}),
            **self.not_important_args,
        )

        record = self.sensitive_filter.filter(record)

        assert (
            record.getMessage() == "This should {'also': 'work', 'not': '<<REDACTED>>'}"
        )

    def test_dict_log_record_sensitive_keyname(self):
        record = logging.LogRecord(
            msg="creds %s",
            args=(
                {
                    "user": "alice",
                    "token": "uc-4aefafdase8",
                    "private_token": "uc-CVFGFFEEE",
                    "secret": "shh",
                    "api_key": "abc123",
                    "password": "hunter2",
                    "note": "ok",
                },
            ),
            **self.not_important_args,
        )
        record = self.sensitive_filter.filter(record)
        assert record.getMessage() == (
            "creds {'user': 'alice', "
            "'token': '<<REDACTED>>', "
            "'private_token': '<<REDACTED>>', "
            "'secret': '<<REDACTED>>', "
            "'api_key': '<<REDACTED>>', "
            "'password': '<<REDACTED>>', "
            "'note': 'ok'}"
        )

    def test_url_redaction(self):
        record = logging.LogRecord(
            msg="Bad https://user:uc-4aefafdase8@unfurl.cloud/onecommons/?private_token=uc-4aefafdase8 %s %s",
            args=("bad https://root:uc-CVFGFFEEE@unfurl.cloud/onecommons/?token=bad&arg=1 ", "ok https://user2@unfurl.cloud/onecommons/?private_token=uc-4aefafdase8"),
            **self.not_important_args,
        )
        record = self.sensitive_filter.filter(record)

        assert (
            record.getMessage() == "Bad https://user:XXXXX@unfurl.cloud/onecommons/?private_token=XXXXX bad https://root:XXXXX@unfurl.cloud/onecommons/?token=XXXXX&arg=1  ok https://user2@unfurl.cloud/onecommons/?private_token=XXXXX"
        )

    def test_arg_redaction(self):
        record = logging.LogRecord(
            msg="foo --token=uc-4aefafdase8 %s",
            args=(" --kube-token adfasdfasdfeeref blah",),
            **self.not_important_args,
        )
        record = self.sensitive_filter.filter(record)

        assert (
            record.getMessage() == "foo --token=XXXXX  --kube-token XXXXX blah"
        )

        record = logging.LogRecord(
            msg="foo --token=%s",
            args=("uc-4aefafdase8",),
            **self.not_important_args,
        )
        record = self.sensitive_filter.filter(record)
        assert record.msg == "foo --token=%s"
        assert record.args == ("XXXXX",)

        # Test multiple format specifiers - only the sensitive one should be redacted
        record = logging.LogRecord(
            msg="user %s connecting with --token=%s to %s",
            args=("alice", "secret-token-123", "server.example.com"),
            **self.not_important_args,
        )
        record = self.sensitive_filter.filter(record)
        assert record.msg == "user %s connecting with --token=%s to %s"
        assert record.args == ("alice", "XXXXX", "server.example.com")

        # Test format specifier before sensitive position
        record = logging.LogRecord(
            msg="%s --password=%s",
            args=("command", "my-password"),
            **self.not_important_args,
        )
        record = self.sensitive_filter.filter(record)
        assert record.msg == "%s --password=%s"
        assert record.args == ("command", "XXXXX")

        # Test multiple sensitive positions
        record = logging.LogRecord(
            msg="--secret=%s and --token=%s for user %s",
            args=("secret123", "token456", sensitive_str("bob")),
            **self.not_important_args,
        )
        record = self.sensitive_filter.filter(record)
        assert record.msg == "--secret=%s and --token=%s for user %s"
        assert record.args == ("XXXXX", "XXXXX", "<<REDACTED>>")

        # Test non-sensitive arguments are not redacted
        record = logging.LogRecord(
            msg="normal arg %s and another %s",
            args=(sensitive_str("value1"), "value2"),
            **self.not_important_args,
        )
        record = self.sensitive_filter.filter(record)
        assert record.msg == "normal arg %s and another %s"
        assert record.args == ("<<REDACTED>>", "value2")

        record = logging.LogRecord(
            msg="url '%s'",
            args=("https://github:uc-DEADBEAF@unfurl.cloud/",),
            **self.not_important_args,
        )
        record = self.sensitive_filter.filter(record)
        assert record.msg == "url '%s'"
        assert record.args == ("https://github:XXXXX@unfurl.cloud/",)


class TestColorHandler:
    def test_exception_is_printed(self, caplog):
        log = logging.getLogger("test_exception_is_printed")

        try:
            raise ValueError("for fun")
        except ValueError as e:
            log.error("I caught an error: %s", e, exc_info=True)

        assert "Traceback (most recent call last):" in caplog.text

def test_log_once_basic(caplog_with_filters):
    """Test that messages with log_once=True are only logged once."""
    logger = getLogger("test_log_once_basic")

    with caplog_with_filters.at_level(logging.INFO):
        # First log should appear
        logger.info("Test message", extra={"log_once": True})
        assert "Test message" in caplog_with_filters.text
        caplog_with_filters.clear()

        # Second identical log should be filtered out
        logger.info("Test message", extra={"log_once": True})
        assert caplog_with_filters.text == ""
        caplog_with_filters.clear()

        # Different message should appear
        logger.info("Different message", extra={"log_once": True})
        assert "Different message" in caplog_with_filters.text


def test_log_once_with_args(caplog_with_filters):
    """Test that log_once considers message arguments."""
    logger = getLogger("test_log_once_args")

    with caplog_with_filters.at_level(logging.INFO):
        # First log with args
        logger.info("Message with %s", "arg1", extra={"log_once": True})
        assert "arg1" in caplog_with_filters.text
        caplog_with_filters.clear()

        # Same message, same args - should be filtered
        logger.info("Message with %s", "arg1", extra={"log_once": True})
        assert caplog_with_filters.text == ""
        caplog_with_filters.clear()

        # Same message, different args - should appear
        logger.info("Message with %s", "arg2", extra={"log_once": True})
        assert "arg2" in caplog_with_filters.text


def test_log_once_multiple_args(caplog_with_filters):
    """Test log_once with multiple arguments."""
    logger = getLogger("test_log_once_multiple")

    with caplog_with_filters.at_level(logging.INFO):
        # First log
        logger.info("Values: %s, %s, %d", "a", "b", 123, extra={"log_once": True})
        assert "Values:" in caplog_with_filters.text
        caplog_with_filters.clear()

        # Duplicate - should be filtered
        logger.info("Values: %s, %s, %d", "a", "b", 123, extra={"log_once": True})
        assert caplog_with_filters.text == ""
        caplog_with_filters.clear()

        # Different values - should appear
        logger.info("Values: %s, %s, %d", "a", "b", 456, extra={"log_once": True})
        assert "Values:" in caplog_with_filters.text


def test_log_once_mixed_with_normal(caplog_with_filters):
    """Test mixing log_once messages with normal messages."""
    logger = getLogger("test_log_once_mixed")

    with caplog_with_filters.at_level(logging.INFO):
        # Normal message
        logger.info("Normal message 1")
        assert "Normal message 1" in caplog_with_filters.text
        caplog_with_filters.clear()

        # log_once message
        logger.info("Once message", extra={"log_once": True})
        assert "Once message" in caplog_with_filters.text
        caplog_with_filters.clear()

        # Repeat normal message - should appear
        logger.info("Normal message 1")
        assert "Normal message 1" in caplog_with_filters.text
        caplog_with_filters.clear()

        # Repeat log_once message - should be filtered
        logger.info("Once message", extra={"log_once": True})
        assert caplog_with_filters.text == ""


def test_log_once_different_levels(caplog_with_filters):
    """Test that log_once works across different log levels."""
    logger = getLogger("test_log_once_levels")

    with caplog_with_filters.at_level(logging.INFO):
        # Info level
        logger.info("Test at info", extra={"log_once": True})
        assert "Test at info" in caplog_with_filters.text
        caplog_with_filters.clear()

        # Same message at info level - should be filtered
        logger.info("Test at info", extra={"log_once": True})
        assert caplog_with_filters.text == ""
        caplog_with_filters.clear()

        # Warning level with different message
        logger.warning("Test at warning", extra={"log_once": True})
        assert "Test at warning" in caplog_with_filters.text
        caplog_with_filters.clear()

        # Repeat warning - should be filtered
        logger.warning("Test at warning", extra={"log_once": True})
        assert caplog_with_filters.text == ""


def test_log_once_with_dict_args(caplog_with_filters):
    """Test log_once with dictionary-style arguments."""
    logger = getLogger("test_log_once_dict")

    with caplog_with_filters.at_level(logging.INFO):
        # First log with dict args
        logger.info("Message: %(key)s", {"key": "value1"}, extra={"log_once": True})
        assert "value1" in caplog_with_filters.text
        caplog_with_filters.clear()

        # Duplicate - should be filtered
        logger.info("Message: %(key)s", {"key": "value1"}, extra={"log_once": True})
        assert caplog_with_filters.text == ""
        caplog_with_filters.clear()

        # Different value - should appear
        logger.info("Message: %(key)s", {"key": "value2"}, extra={"log_once": True})
        assert "value2" in caplog_with_filters.text


def test_log_once_with_complex_objects(caplog_with_filters):
    """Test log_once with complex objects as arguments."""
    logger = getLogger("test_log_once_objects")

    # Objects will be converted to strings for comparison
    obj1 = {"data": [1, 2, 3]}
    obj2 = {"data": [1, 2, 3]}
    obj3 = {"data": [4, 5, 6]}

    with caplog_with_filters.at_level(logging.INFO):
        # First log with obj1
        logger.info("Object: %s", obj1, extra={"log_once": True})
        assert "Object:" in caplog_with_filters.text
        caplog_with_filters.clear()

        # Log with obj2 (equal to obj1 but different instance)
        # Should be filtered because string representation is the same
        logger.info("Object: %s", obj2, extra={"log_once": True})
        assert caplog_with_filters.text == ""
        caplog_with_filters.clear()

        # Log with obj3 (different content) - should appear
        logger.info("Object: %s", obj3, extra={"log_once": True})
        assert "Object:" in caplog_with_filters.text


def test_logger_has_log_once_filter():
    """Test that loggers from getLogger() have LogOnceFilter configured automatically."""
    from io import StringIO
    import uuid

    # Verify the LogOnceFilter is configured on root logger handlers
    root_logger = logging.getLogger()
    log_once_filter = None
    for h in root_logger.handlers:
        for f in h.filters:
            if isinstance(f, LogOnceFilter):
                log_once_filter = f
                break
        if log_once_filter:
            break

    assert log_once_filter is not None, (
        "LogOnceFilter should be configured on root logger handlers"
    )

    # Get an unmodified logger from getLogger()
    logger = getLogger("test_unfurl_logger_unmodified")

    # Add a StringIO handler with the same filter to capture output
    log_stream = StringIO()
    handler = logging.StreamHandler(log_stream)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(log_once_filter)  # Use the same filter instance from root
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    old_propagate = logger.propagate
    logger.propagate = False  # Don't propagate to avoid duplicate handling

    try:
        # Use unique messages to avoid collision with other tests
        unique_msg = "Unique test message %s %s"

        # First log should appear
        logger.info(unique_msg, "a", "b", extra={"log_once": True})
        output1 = log_stream.getvalue()
        msg = unique_msg % ("a", "b")
        assert msg in output1, repr(msg) + " should be logged in " + output1
        log_stream.truncate(0)
        log_stream.seek(0)

        # Second identical log should be filtered out
        logger.info(unique_msg, "a", "b", extra={"log_once": True})
        # Different message should appear
        unique_msg2 = "Different unique message"
        logger.info(unique_msg2, extra={"log_once": True})
        output3 = log_stream.getvalue()
        assert msg not in output3, (
            "Second identical message should be filtered by LogOnceFilter"
        )
        assert unique_msg2 in output3, "Different message should be logged"
    finally:
        logger.removeHandler(handler)
        logger.propagate = old_propagate
        log_stream.close()


def test_make_key_with_none_args():
    """Test the _make_key method with None arguments."""
    key1 = LogOnceFilter._make_key("message", None)
    key2 = LogOnceFilter._make_key("message", None)
    assert key1 == key2

    key3 = LogOnceFilter._make_key("different", None)
    assert key1 != key3


def test_make_key_with_tuple_args():
    """Test the _make_key method with tuple arguments."""
    key1 = LogOnceFilter._make_key("msg", ("a", "b", 123))
    key2 = LogOnceFilter._make_key("msg", ("a", "b", 123))
    assert key1 == key2

    key3 = LogOnceFilter._make_key("msg", ("a", "b", 456))
    assert key1 != key3


def test_make_key_with_dict_args():
    """Test the _make_key method with dictionary arguments."""
    key1 = LogOnceFilter._make_key("msg", {"x": 1, "y": 2})
    key2 = LogOnceFilter._make_key("msg", {"y": 2, "x": 1})  # Different order
    assert key1 == key2  # Should be equal due to sorting

    key3 = LogOnceFilter._make_key("msg", {"x": 1, "y": 3})
    assert key1 != key3


from unfurl.reporting import JobTable, Console

def summary_table():
    # title = "Job %s completed in %.3fs: [%s]%s[/]. %s:\n    " % (
    #     self.changeId,
    #     self.timeElapsed,
    #     self.status.color,
    #     self.status.name,
    #     self.stats(asMessage=True),
    # )
    title = "Test"
    table = JobTable(title=title)

    table.add_column(" ", justify="right", style="cyan", no_wrap=True)
    table.add_column("Operation", style="magenta")
    table.add_column("Reason", style="magenta")
    # table.add_column("Resource", style="magenta")
    # table.add_column("Status", style="magenta")
    # table.add_column("Changed", style="magenta")


    # table.add_column("", justify="right", style="green")

    table.add_row("Dec 20, 2019", "Star Wars: The Rise of Skywalker", "952", extra="This is some Text"*10)
    table.add_row("May 25, 2018", "Solo: A Star Wars Story", "[red]3945[/]", extra="More [b]hacky[/b] text")

    # table.add_row("Dec 15, 2017", "Star Wars Ep. V111: The Last Jedi", "$1,332,539,889")
    # table.add_row("Dec 16, 2016", "Rogue One: A Star Wars Story", "$1,332,439,889")

    console = getConsole(record=True)
    console.print(table)

if __name__ == "__main__":
    logger = logs.getLogger("unfurl")
    logs.set_console_log_level(Levels.TRACE)
    logger.error("redacted %s", sensitive_str("sensitive"))
    for msg, level in [
        ("CRITICAL", Levels.CRITICAL),
        ("ERROR", Levels.ERROR),
        ("WARNING", Levels.WARNING),
        ("INFO", Levels.INFO),
        ("VERBOSE", Levels.VERBOSE),
        ("DEBUG", Levels.DEBUG),
        ("TRACE", Levels.TRACE),
    ]:
        logger.log(level, msg)
    summary_table()

