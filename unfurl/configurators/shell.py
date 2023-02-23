# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
"""
environment:
timeout:
inputs:
 command: "--switch {{ '.::foo' | ref }}"
 cwd
 dryrun
 shell
 keeplines
 done
 resultTemplate: # available vars: cmd, stdout, stderr, returncode, error, timeout
   eval:
    file:
      ./handleResult.tpl
   select: contents # get the file contents
"""


# see also 13.4.1 Shell scripts p 360
# XXX add support for a stdin parameter

from ..configurator import Status
from ..util import which, truncate_str, clean_output
from . import TemplateConfigurator
import os
import sys
import shlex
import six
import re
from typing import Optional

import subprocess


def make_regex_filter(logregex: re.Pattern, levels: list):
    def filter(data: str, skip: bool):
        bare_line = clean_output(data)
        match = logregex.search(bare_line)
        loglevel = match and match.group(1)
        if loglevel is not None:
            # its a new log message
            return loglevel not in levels
        return skip

    return filter


class _PrintOnAppendList(list):
    def __init__(self, filter=None):
        self._filter = filter
        self.skip = False

    def filter(self, data: str):
        if self._filter:
            self.skip = self._filter(data, self.skip)
            if self.skip:
                return None
        return data

    def append(self, data):
        list.append(self, data)
        try:
            s = self.filter(data.decode())
            if s is not None:
                sys.stdout.write(s)
        except Exception:
            if os.environ.get("UNFURL_RAISE_LOGGING_EXCEPTIONS"):
                raise


def _run(*args, stdout_filter=None, stderr_filter=None, **kwargs):
    timeout = kwargs.pop("timeout", None)
    input = kwargs.pop("input", None)
    with subprocess.Popen(*args, **kwargs) as process:
        try:
            stdout = None
            stderr = None
            _save_input = process._save_input  # type: ignore
            # _save_input is called after _fileobj2output is setup but before reading
            def _save_input_hook_hack(input):
                if process.stdout:
                    process._fileobj2output[process.stdout] = _PrintOnAppendList(stdout_filter)  # type: ignore
                if process.stderr:
                    process._fileobj2output[process.stderr] = _PrintOnAppendList(stderr_filter)  # type: ignore
                _save_input(input)

            process._save_input = _save_input_hook_hack  # type: ignore
            process.communicate(input, timeout=timeout)
            if process.stdout:
                stdout = b"".join(process._fileobj2output[process.stdout])  # type: ignore
            if process.stderr:
                stderr = b"".join(process._fileobj2output[process.stderr])  # type: ignore
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise
        except:  # Including KeyboardInterrupt, communicate handled that.
            process.kill()
            # We don't call process.wait() as .__exit__ does that for us.
            raise
        retcode = process.poll()
        assert isinstance(retcode, int)
    return subprocess.CompletedProcess(process.args, retcode, stdout, stderr)


# XXX we should know if cmd if not os.access(implementation, os.X):
class ShellConfigurator(TemplateConfigurator):
    exclude_from_digest = TemplateConfigurator.exclude_from_digest + ("cwd", "echo")
    _default_cmd: Optional[str] = None
    _default_dryrun_arg: Optional[str] = None

    @staticmethod
    def _cmd(cmd, keeplines):
        if not isinstance(cmd, six.string_types):
            cmdStr = " ".join(cmd)
        else:
            if not keeplines:
                cmd = cmd.replace("\n", " ")
            cmdStr = cmd
            cmd = shlex.split(cmd)
        return cmdStr, cmd

    def run_process(
        self,
        cmd,
        shell=False,
        timeout=None,
        env=None,
        cwd=None,
        keeplines=False,
        echo=True,
        stdout_filter=None,
        stderr_filter=None,
    ):
        """
        Returns an object with the following attributes:

        cmd
        timeout (None unless timeout occurred)
        stderr
        stdout
        returncode (None if the process didn't complete)
        error if an exception was raised
        """
        cmdStr, cmd = self._cmd(cmd, keeplines)
        try:
            # hack to echo results
            if echo and hasattr(subprocess.Popen, "_save_input"):
                run = _run
                kwargs = dict(stdout_filter=stdout_filter, stderr_filter=stderr_filter)
            else:
                # Windows and 2.7 don't have _save_input
                run = subprocess.run  # type: ignore
                kwargs = {}
            if shell and isinstance(shell, six.string_types):
                executable = shell
            else:
                executable = None
            completed = run(
                # follow recommendation to use string with shell, list without
                cmdStr if shell else cmd,
                shell=shell,
                executable=executable,
                env=env,
                cwd=cwd,
                timeout=timeout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                input=None,
                **kwargs,
            )

            # try to convert stdout and stderr to strings but leave as binary if that fails
            try:
                completed.stdout = completed.stdout.decode()
            except:
                pass
            try:
                completed.stderr = completed.stderr.decode()
            except:
                pass
            completed.cmd = cmdStr
            completed.timeout = None
            completed.error = None
            return completed
        except subprocess.TimeoutExpired as err:
            err.cmd = cmdStr
            err.timeout = timeout
            err.returncode = None  # type: ignore
            err.error = None  # type: ignore
            return err
        except Exception as err:
            err.cmd = cmdStr  # type: ignore
            err.timeout = None  # type: ignore
            err.stderr = None  # type: ignore
            err.stdout = None  # type: ignore
            err.returncode = None  # type: ignore
            err.error = err  # type: ignore
            return err

    def _handle_result(self, task, result, cwd, successCodes=(0,)):
        # strips terminal escapes
        result.stdout = clean_output(result.stdout or "")
        result.stderr = clean_output(result.stderr or "")
        error = result.error or result.returncode not in successCodes or result.timeout
        if error:
            task.logger.warning('shell task run failure: "%s" in %s', result.cmd, cwd)
            if result.error:
                task.logger.info("shell task error", exc_info=result.error)
            else:
                task.logger.info(
                    "shell task return code: %s, stderr: %s",
                    result.returncode,
                    truncate_str(result.stderr),
                )
        else:
            task.logger.info("shell task run success: %s", result.cmd)
            task.logger.debug("shell task output: %s", truncate_str(result.stdout))
        return not error

    def _process_result(self, task, result, cwd):
        success = self._handle_result(task, result, cwd)
        resultDict = result.__dict__.copy()
        resultDict["success"] = success
        errors, status = self.process_result_template(task, resultDict)
        if task._errors:
            return False, status
        return success, status

    def can_run(self, task):
        params = task.inputs
        cmd = params.get("command", self._default_cmd)
        if not cmd:
            return "missing command to execute"
        if isinstance(cmd, list) and not params.get("shell") and not which(cmd[0]):
            return f"'{cmd[0]}' is not executable"
        return True

    def can_dry_run(self, task):
        return task.inputs.get("dryrun")

    def render(self, task):
        params = task.inputs
        cmd = params["command"]
        cwd = params.get("cwd")
        if cwd:
            # if cwd is relative, make it relative to task.cwd
            assert isinstance(cwd, six.string_types)
            cwd = os.path.abspath(os.path.join(task.cwd, cwd))
        else:
            cwd = task.cwd
        cmd = self.resolve_dry_run(cmd, task)
        return [cmd, cwd]

    def run(self, task):
        cmd, cwd = task.rendered
        task.logger.trace("executing %s", cmd)
        params = task.inputs
        isString = isinstance(cmd, six.string_types)
        # default for shell: True if command is a string otherwise False
        shell = params.get("shell", isString)
        env = task.environ
        task.logger.trace("shell using env %s", env)
        keeplines = params.get("keeplines")
        echo = params.get("echo", task.verbose > -1)
        result = self.run_process(
            cmd,
            shell=shell,
            timeout=task.configSpec.timeout,
            env=env,
            cwd=cwd,
            keeplines=keeplines,
            echo=echo,
        )
        success, status = self._process_result(task, result, cwd)
        yield self.done(task, success=success, status=status, result=result.__dict__)

    def resolve_dry_run(self, cmd, task):
        is_string = isinstance(cmd, six.string_types)
        dry_run_arg = task.inputs.get("dryrun", self._default_dryrun_arg)
        if task.dry_run and isinstance(dry_run_arg, six.string_types):
            if "%dryrun%" in cmd:  # replace %dryrun%
                if is_string:
                    cmd = cmd.replace("%dryrun%", dry_run_arg)
                else:
                    cmd[cmd.index("%dryrun%")] = dry_run_arg
            else:  # append dry_run_arg
                if is_string:
                    cmd += " " + dry_run_arg
                else:
                    cmd.append(dry_run_arg)
        elif "%dryrun%" in cmd:
            if is_string:
                cmd = cmd.replace("%dryrun%", "")
            else:
                cmd.remove("%dryrun%")
        return cmd
