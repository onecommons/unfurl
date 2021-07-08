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


def test_format_of_job_file_log():
    cli_runner = CliRunner()
    with cli_runner.isolated_filesystem():
        path = Path(__file__).parent / "examples" / "shell-ensemble.yaml"
        with open(path) as f:
            ensemble = f.read()
        with open("ensemble.yaml", "w") as f:
            f.write(ensemble)
        manifest = LocalEnv().get_manifest()
        runner = Runner(manifest)

        runner.run(JobOptions(instance="test1"))
        log_file = list((Path.cwd() / "jobs").glob("*.log"))[0]

        with open(log_file) as f:
            first_line = f.readline()
            # check format - it should differ from console log
            expected_format = r"\[.+\] unfurl:INFO: starting deploy job for .*"
            assert re.match(expected_format, first_line)


class TestLogLevelDetection:
    def setup(self):
        self.old_env_level = os.environ.get("UNFURL_LOGGING")
        if self.old_env_level:
            del os.environ["UNFURL_LOGGING"]

    def teardown(self):
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

    def setup(self):
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

