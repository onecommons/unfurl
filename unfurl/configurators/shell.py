# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
"""
environment:
timeout:
inputs:
 command: "--switch {{ '.::foo' | eval }}"
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

from ..eval import map_value
from ..logs import truncate, DEFAULT_TRUNCATE_LENGTH
from ..configurator import Status, TaskView
from ..util import which, clean_output
from . import TemplateConfigurator, TemplateInputs
from ansible.utils.unsafe_proxy import AnsibleUnsafeText
import os
import sys
import shlex
import re
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
    Union,
    cast,
    TYPE_CHECKING,
)
import subprocess

if TYPE_CHECKING:
    from ..job import ConfigTask

# logging to file doesn't call logging.truncate(), so manually truncate potentially huge output
FILELOG_TRUNCATE_LENGTH = DEFAULT_TRUNCATE_LENGTH

def _log_output(task, result, attr: str):
    data = getattr(result, attr)
    if len(data) > FILELOG_TRUNCATE_LENGTH:
        log_path = task.job.log_path(ext=f"-{task.target.name}-{attr}.log")
        dir = os.path.dirname(log_path)
        if not os.path.exists(dir):
            os.makedirs(dir)
        with open(log_path, 'a') as f:
            f.write(data)
        return f"{attr} {data[: FILELOG_TRUNCATE_LENGTH // 2]}... full output logged to {log_path}"
    else:
        return data


class ShellInputs(TemplateInputs):
    command: Union[None, str, List[str]] = None
    shell: Union[None, str, bool] = None
    "If shell is None, default to True if command is a string otherwise False"
    cwd: Union[None, str] = None
    keeplines: bool = False
    echo: Union[None, bool] = None
    "Echo output, default depends on job verbosity"
    input: Union[None, str] = None
    "Optional string to pass as stdin."


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


def _run(
    *args, stdout_filter=None, stderr_filter=None, input=None, timeout=None, **kwargs
):
    with subprocess.Popen(*args, **kwargs) as process:
        try:
            stdout = None
            stderr = None
            _save_input = process._save_input  # type: ignore

            # _save_input is called after _fileobj2output is setup but before reading
            def _save_input_hook_hack(input):
                if process.stdout:
                    process._fileobj2output[process.stdout] = _PrintOnAppendList(  # type: ignore
                        stdout_filter
                    )
                if process.stderr:
                    process._fileobj2output[process.stderr] = _PrintOnAppendList(  # type: ignore
                        stderr_filter
                    )
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
        if not isinstance(cmd, str):
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
        shell: Union[None, str, bool] = False,
        timeout=None,
        env=None,
        cwd=None,
        keeplines=False,
        echo=True,
        stdout_filter=None,
        stderr_filter=None,
        input=None,
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
            if shell and isinstance(shell, str):
                executable = shell
                use_shell = True
            else:
                use_shell = bool(shell)
                executable = None
            if input is not None:
                kwargs["stdin"] = subprocess.PIPE
                if isinstance(input, str):
                    input = input.encode()
            completed = run(
                # follow recommendation to use string with shell, list without
                cmdStr if use_shell else cmd,
                shell=use_shell,
                executable=executable,
                env=env,
                cwd=cwd,
                timeout=timeout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                input=input,
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

    def _handle_result(self, task: TaskView, result, cwd, successCodes=(0,)):
        # strips terminal escapes
        result.stdout = AnsibleUnsafeText(clean_output(result.stdout or ""))
        result.stderr = AnsibleUnsafeText(clean_output(result.stderr or ""))
        error = result.error or result.returncode not in successCodes or result.timeout
        if error:
            task.logger.warning('shell task run failure: "%s" in %s', result.cmd, cwd)
            if result.error:
                task.logger.info("shell task error", exc_info=result.error)
            else:
                task.logger.info(
                    "shell task return code: %s, stderr: %s",
                    result.returncode,
                    _log_output(task, result, "stderr"),
                )
        else:
            task.logger.info("shell task run success: %s", result.cmd)
            task.logger.debug(
                "shell task output: %s",
                _log_output(task, result, "stdout"),
            )
        return not error

    def _process_outputs(self, task: "TaskView", result: Dict[str, Any]):
        tpl = task.inputs.get_original("outputsTemplate")
        if tpl is None:
            return None, None
        try:
            return None, map_value(tpl, task.inputs.context.copy(vars=result))
        except Exception as e:
            task.logger.warning("error processing outputsTemplate: %s", e)
            return e, None

    def _process_result(
        self, task: TaskView, result, cwd: str
    ) -> Tuple[bool, Optional[Status], Optional[Dict[str, Any]]]:
        success = self._handle_result(task, result, cwd)
        resultDict = result.__dict__.copy()
        resultDict["success"] = success
        errors, status = self.process_result_template(task, resultDict)
        if errors:
            return False, status, None
        error, outputs = self._process_outputs(task, resultDict)
        return success and not error, status, outputs

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

    def render(self, task: TaskView):
        cmd = task.inputs["command"]
        cwd = task.inputs.get("cwd")
        if cwd:
            # if cwd is relative, make it relative to task.cwd
            assert isinstance(cwd, str)
            cwd = os.path.abspath(os.path.join(task.cwd, cwd))
        else:
            cwd = task.cwd
        cmd = self.resolve_dry_run(cmd, task)
        isString = isinstance(cmd, str)
        # default for shell: True if command is a string otherwise False
        shell = task.inputs.get("shell", isString)
        if (isString and " " not in cmd) or (not isString and len(cmd) == 1):
            # if cmd is a single command append arguments (otherwise assume they were processed)
            arguments = task.inputs.get_copy("arguments")
            if arguments:
                args = [
                    f"{'--' if name[0] != '-' else ''}{name} {shlex.quote(str(value)) if shell else value}"
                    for name, value in arguments.items()
                ]
                if isString:
                    cmd += " " + " ".join(args)
                else:
                    cmd.extend(args)
        if isString:
            script = cmd
        else:
            script = " ".join(cmd)
        # save as script just for troubleshooting
        task.set_work_folder().write_file(script, "rendered.sh")
        # try this now to catch errors early:
        _, _ = self._cmd(cmd, task.inputs.get("keeplines", False))
        return [cmd, cwd]

    def run(self, task: TaskView):
        cmd, cwd = task.rendered
        task.logger.trace("executing %s", cmd)
        params = task.inputs
        isString = isinstance(cmd, str)
        # default for shell: True if command is a string otherwise False
        shell = params.get("shell", isString)
        env = task.environ
        task.logger.trace("shell using env %s", env)
        keeplines = params.get("keeplines", False)
        echo = params.get("echo", cast("ConfigTask", task).verbose > -1)
        input = params.get("input")
        result = self.run_process(
            cmd,
            shell=shell,
            timeout=task.configSpec.timeout,
            env=env,
            cwd=cwd,
            keeplines=keeplines,
            echo=echo,
            input=input,
        )
        success, status, outputs = self._process_result(task, result, cwd)
        yield self.done(
            task,
            success=success,
            status=status,
            result=result.__dict__,
            outputs=outputs,
        )

    def resolve_dry_run(self, cmd, task):
        is_string = isinstance(cmd, str)
        dry_run_arg = task.inputs.get("dryrun", self._default_dryrun_arg)
        if task.dry_run and isinstance(dry_run_arg, str):
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
