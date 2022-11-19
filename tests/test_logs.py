import logging
import os
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from unfurl.__main__ import detect_log_level, detect_verbose_level
from unfurl.job import JobOptions, Runner
from unfurl.localenv import LocalEnv
from unfurl.logs import Levels, SensitiveFilter
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

        self.sensitive_filter.filter(record)

        assert record.getMessage() == "This should work, <<REDACTED>>"

    def test_dict_log_record(self):
        record = logging.LogRecord(
            msg="This should %s",
            args=({"also": "work", "not": sensitive_str("not this")}),
            **self.not_important_args,
        )

        self.sensitive_filter.filter(record)

        assert (
            record.getMessage() == "This should {'also': 'work', 'not': '<<REDACTED>>'}"
        )


class TestColorHandler:
    def test_exception_is_printed(self, caplog):
        log = logging.getLogger("test_exception_is_printed")

        try:
            raise ValueError("for fun")
        except ValueError as e:
            log.error("I caught an error: %s", e, exc_info=True)

        assert "Traceback (most recent call last):" in caplog.text

def test_redaction(caplog):
    logger = logging.getLogger("unfurl")
    logger.info("should be redacted %s", sensitive_str("password"))
    assert "password" not in caplog.text
    logger.info("should be redacted %s", CommentedMap(key=CommentedMap(nested=sensitive_str("password"))))
    assert "key" in caplog.text
    assert "password" not in caplog.text



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

    table.add_row("Dec 20, 2019", "Star Wars: The Rise of Skywalker", "952", extra="This is some Text")
    table.add_row("May 25, 2018", "Solo: A Star Wars Story", "[red]3945[/]", extra="More [b]hacky[/b] text")

    # table.add_row("Dec 15, 2017", "Star Wars Ep. V111: The Last Jedi", "$1,332,539,889")
    # table.add_row("Dec 16, 2016", "Rogue One: A Star Wars Story", "$1,332,439,889")

    console = Console()
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

