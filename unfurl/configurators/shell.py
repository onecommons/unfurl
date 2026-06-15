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

import selectors
import time

from ..eval import map_value
from ..logs import truncate, DEFAULT_TRUNCATE_LENGTH
from ..util import wrap_sensitive_value
from ..configurator import Cancel, ConfiguratorResult, Status, TaskView
from ..util import which, clean_output
from . import TemplateConfigurator, TemplateInputs
from ansible.utils.unsafe_proxy import AnsibleUnsafeText
import os
import sys
import shlex
import re
import types
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


def _terminate_process(proc: subprocess.Popen, grace_period: float = 5) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=grace_period)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _log_output(task: TaskView, result, attr: str):
    data = getattr(result, attr)
    if (
        task.job
        and not task.job.jobOptions.skip_save
        and len(data) > FILELOG_TRUNCATE_LENGTH
    ):
        log_path = task.job.log_path(ext=f"-{task.target.name}-{attr}.log")
        dir = os.path.dirname(log_path)
        if not os.path.exists(dir):
            os.makedirs(dir)
        with open(log_path, "a") as f:
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


class _BackgroundReader:
    """Live-echo + accumulator for a background subprocess's stdout/stderr.

    Call ``pump()`` from the poll loop to drain whatever is currently
    available (non-blocking), echo it to sys.stdout/sys.stderr, and
    accumulate it. ``drain()`` after the process exits flushes any
    remaining bytes. POSIX-only.

    Optional ``stdout_filter``/``stderr_filter`` callables are the same
    ``(data, skip) -> bool`` shape used by ``_PrintOnAppendList``: each
    chunk is passed through the filter, which returns the new ``skip``
    state; chunks while skipping are accumulated but not echoed.
    """

    def __init__(
        self,
        proc: subprocess.Popen,
        *,
        stdout_filter: Optional[Callable[[str, bool], bool]] = None,
        stderr_filter: Optional[Callable[[str, bool], bool]] = None,
    ) -> None:
        self.stdout_chunks: List[bytes] = []
        self.stderr_chunks: List[bytes] = []
        self._sel = selectors.DefaultSelector()
        # per-fileobj filter state: (sink, buf, filter, skip_flag_list)
        for stream, buf, sink, filt in (
            (proc.stdout, self.stdout_chunks, sys.stdout, stdout_filter),
            (proc.stderr, self.stderr_chunks, sys.stderr, stderr_filter),
        ):
            if stream is None:
                continue
            os.set_blocking(stream.fileno(), False)
            # skip flag wrapped in a list so we can mutate it from pump()
            self._sel.register(stream, selectors.EVENT_READ, (buf, sink, filt, [False]))

    def pump(self, timeout: float = 0.0) -> None:
        for key, _ in self._sel.select(timeout):
            buf, sink, filt, skip_box = key.data
            try:
                chunk = key.fileobj.read()  # type: ignore[union-attr]
            except (BlockingIOError, OSError):
                continue
            if not chunk:  # EOF — pipe closed
                self._sel.unregister(key.fileobj)
                continue
            buf.append(chunk)
            try:
                text = chunk.decode(errors="replace")
                if filt is not None:
                    skip_box[0] = filt(text, skip_box[0])
                    if skip_box[0]:
                        continue
                sink.write(text)
                sink.flush()
            except Exception:
                if os.environ.get("UNFURL_RAISE_LOGGING_EXCEPTIONS"):
                    raise

    def drain(self) -> None:
        # process has exited; keep pumping until both pipes hit EOF.
        while self._sel.get_map():
            ready = self._sel.select(timeout=0.1)
            if not ready:
                break  # nothing more arriving; bail
            self.pump()

    def close(self) -> None:
        self._sel.close()

    def stdout(self) -> bytes:
        return b"".join(self.stdout_chunks)

    def stderr(self) -> bytes:
        return b"".join(self.stderr_chunks)


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
    exclude_from_digest = TemplateConfigurator.exclude_from_digest + (
        "cwd",  # depends on local configuration
        "echo",  # only affects output
        "outputsTemplate",  # only affects output
    )
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

    @staticmethod
    def _popen_args(
        cmd,
        shell: Union[None, str, bool],
        env: Optional[Dict[str, str]],
        cwd: Optional[str],
        input,
        keeplines: bool,
    ) -> Tuple[str, Any, Dict[str, Any], Optional[bytes]]:
        """Normalize cmd + shell + input into the form needed to spawn a
        subprocess. Returns (cmd_str, popen_arg, popen_kwargs, input_bytes)
        where popen_arg is the first positional arg for Popen/run (string for
        shell mode, list otherwise) and input_bytes is the encoded stdin.
        """
        cmd_str, cmd_list = ShellConfigurator._cmd(cmd, keeplines)
        if shell and isinstance(shell, str):
            use_shell = True
            executable: Optional[str] = shell
        else:
            use_shell = bool(shell)
            executable = None
        kwargs: Dict[str, Any] = dict(
            shell=use_shell,
            executable=executable,
            env=env,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        input_bytes: Optional[bytes]
        if input is None:
            input_bytes = None
        else:
            kwargs["stdin"] = subprocess.PIPE
            input_bytes = input.encode() if isinstance(input, str) else input
        # follow recommendation to use string with shell, list without
        return cmd_str, (cmd_str if use_shell else cmd_list), kwargs, input_bytes

    @staticmethod
    def _finalize_result(result, cmd_str: str, *, timeout=None, error=None):
        """Decode stdout/stderr from bytes (leave as-is on failure) and set
        the cmd/timeout/error attributes the rest of the configurator expects.
        Returns the same object for chaining.
        """
        try:
            result.stdout = result.stdout.decode()
        except Exception:
            pass
        try:
            result.stderr = result.stderr.decode()
        except Exception:
            pass
        result.cmd = cmd_str
        result.timeout = timeout
        result.error = error
        return result

    @staticmethod
    def _log_env(task: TaskView, env, level: str = "trace") -> None:
        if env is None:
            return
        log = getattr(task.logger, level)
        path = env.get("PATH")
        if path:
            log("shell env PATH=%s", path)
        log("shell env: %s", wrap_sensitive_value(env))

    def _dispatch_run(
        self,
        task: TaskView,
        cmd,
        *,
        background: bool,
        timeout=None,
        env=None,
        cwd=None,
        shell: Union[None, str, bool] = False,
        keeplines: bool = False,
        echo: bool = True,
        stdout_filter: Optional[Callable[[str, bool], bool]] = None,
        stderr_filter: Optional[Callable[[str, bool], bool]] = None,
        input=None,
        lock_cwd: bool = False,
    ):
        """Run a subprocess synchronously or in background mode, returning
        the collected result. Always use via ``yield from``::

            result = yield from self._dispatch_run(task, cmd, background=...,
                                                    env=env, cwd=cwd, ...)

        The foreground branch never yields — ``yield from`` on a generator
        that returns without yielding gives the return value immediately, so
        the same caller pattern works for both modes.

        Always returns a result. Cancel/timeout in background mode populate
        ``result.error`` / ``result.timeout`` on the returned result;
        callers should check those (typically via :meth:`_handle_result`
        or :meth:`_process_result`) to decide success/failure.

        Pass ``lock_cwd=True`` to acquire exclusive use of ``cwd`` for the
        duration of the subprocess. If the lock can't be acquired immediately,
        this helper will yield until it can.
        """
        task.logger.trace("executing %s", cmd)
        self._log_env(task, env, "trace")
        if lock_cwd and cwd is not None:
            yield from task.acquire_path(cwd)
        try:
            if background:
                result = yield from self._run_subprocess_background(
                    task,
                    cmd,
                    env=env,
                    cwd=cwd,
                    shell=shell,
                    keeplines=keeplines,
                    echo=echo,
                    stdout_filter=stdout_filter,
                    stderr_filter=stderr_filter,
                    input=input,
                )
                return result
            return self.run_process(
                cmd,
                shell=shell,
                timeout=timeout,
                env=env,
                cwd=cwd,
                keeplines=keeplines,
                echo=echo,
                stdout_filter=stdout_filter,
                stderr_filter=stderr_filter,
                input=input,
            )
        finally:
            if lock_cwd and cwd is not None:
                task.release_path(cwd)

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
        cmd_str, popen_arg, popen_kwargs, input_bytes = self._popen_args(
            cmd, shell, env, cwd, input, keeplines
        )
        try:
            # hack to echo results
            if echo and hasattr(subprocess.Popen, "_save_input"):
                run = _run
                extra_kwargs: Dict[str, Any] = dict(
                    stdout_filter=stdout_filter, stderr_filter=stderr_filter
                )
            else:
                # Windows and 2.7 don't have _save_input
                run = subprocess.run  # type: ignore
                extra_kwargs = {}
                if input_bytes is not None:
                    popen_kwargs.pop("stdin", None)
            completed = run(
                popen_arg,
                timeout=timeout,
                input=input_bytes,
                **popen_kwargs,
                **extra_kwargs,
            )
            return self._finalize_result(completed, cmd_str)
        except subprocess.TimeoutExpired as err:
            err.cmd = cmd_str
            err.timeout = timeout
            err.returncode = None  # type: ignore
            err.error = None  # type: ignore
            return err
        except Exception as err:
            err.cmd = cmd_str  # type: ignore
            err.timeout = None  # type: ignore
            err.stderr = None  # type: ignore
            err.stdout = None  # type: ignore
            err.returncode = None  # type: ignore
            err.error = err  # type: ignore
            return err

    def _handle_result(self, task: TaskView, result, cwd, successCodes=(0,), env=None):
        # strips terminal escapes
        result.stdout = AnsibleUnsafeText(clean_output(result.stdout or ""))
        result.stderr = AnsibleUnsafeText(clean_output(result.stderr or ""))
        error = result.error or result.returncode not in successCodes or result.timeout
        if error:
            task.logger.warning('shell task run failure: "%s" in %s', result.cmd, cwd)
            if result.error:
                task.logger.info("shell task error", exc_info=result.error)
            elif result.timeout:
                task.logger.info("task timed out in %s", result.timeout)
            else:
                task.logger.info("shell task return code: %s", result.returncode)
            self._log_env(task, env, "debug")
        else:
            task.logger.info("shell task run success: %s", result.cmd)
        if result.stderr:
            task.logger.info(
                "shell task stderr: %s",
                _log_output(task, result, "stderr"),
            )
        if result.stdout:
            task.logger.debug(
                "shell task output: %s",
                _log_output(task, result, "stdout"),
            )
        return not error

    def _process_outputs(
        self, task: "TaskView", result: Dict[str, Any]
    ) -> Tuple[Optional[Exception], Optional[Dict[str, Any]]]:
        tpl = task.inputs.get_original("outputsTemplate")
        if tpl is None:
            return None, None
        try:
            # to accommodate navigation through computed properties to reach the template
            # (e.g from the artifact to node owning it) set the result vars to be deep vars.
            ctx = task.inputs.context.copy(deep=result)
            outputs = map_value(tpl, ctx)
            task.logger.debug("processed outputsTemplate:\n %s\nto:\n%s", tpl, outputs)
            return None, outputs
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

    def can_run(self, task) -> Union[bool, str]:
        params = task.inputs
        cmd = params.get("command", self._default_cmd)
        if not cmd:
            return "missing command to execute"
        if isinstance(cmd, list) and not params.get("shell") and not which(cmd[0]):
            return f"'{cmd[0]}' is not executable"
        return True

    def can_dry_run(self, task) -> bool:
        return bool(task.inputs.get("dryrun"))

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
        # try this now to catch errors early:
        script, _ = self._cmd(cmd, task.inputs.get("keeplines", False))
        input = task.inputs.get("input")
        if input is not None:
            eof = "UEOF"
            while eof in input:
                eof += "X"
            script += f" <<'{eof}'\n{input}\n{eof}"
        # save as script just for troubleshooting
        task.set_work_folder().write_file(script, "rendered.sh")
        return [cmd, cwd]

    def _resolve_run_inputs(self, task: TaskView):
        """Pull common run-time params off the task. Returns
        (cmd, cwd, shell, keeplines, input, env). Single chokepoint so
        env-related logging happens in one place."""
        cmd, cwd = task.rendered
        params = task.inputs
        shell = params.get("shell", isinstance(cmd, str))
        keeplines = params.get("keeplines", False)
        env = task.environ
        return cmd, cwd, shell, keeplines, params.get("input"), env

    def run(self, task: TaskView):
        if task.inputs.get("background") or os.environ.get(
            "UNFURL_TEST_SHELL_BACKGROUND"
        ):
            yield from self._run_background(task)
            return
        cmd, cwd, shell, keeplines, input, env = self._resolve_run_inputs(task)
        task.logger.trace("executing %s", cmd)
        self._log_env(task, env, "trace")
        echo = task.inputs.get("echo", cast("ConfigTask", task).verbose > -1)
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

    def _run_background(self, task: TaskView):
        cmd, cwd, shell, keeplines, input, env = self._resolve_run_inputs(task)
        result = yield from self._run_subprocess_background(
            task,
            cmd,
            env=env,
            cwd=cwd,
            shell=shell,
            keeplines=keeplines,
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

    def _run_subprocess_background(
        self,
        task: TaskView,
        cmd,
        *,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
        shell: Union[None, str, bool] = None,
        keeplines: bool = False,
        echo: Optional[bool] = None,
        stdout_filter: Optional[Callable[[str, bool], bool]] = None,
        stderr_filter: Optional[Callable[[str, bool], bool]] = None,
        input=None,
    ):
        """Background analog of ``run_process``. Yields ``task.suspend`` while
        polling the subprocess. Use via::

            result = yield from self._run_subprocess_background(task, cmd, ...)

        ``echo`` / ``stdout_filter`` / ``stderr_filter`` mirror ``run_process``.
        When ``echo`` is ``None`` (the default) the helper falls back to
        ``task.inputs["echo"]`` or ``verbose > -1``.

        Always returns a result. On task timeout, ``result.timeout`` is set
        to the configured timeout and the subprocess is terminated. On
        :class:`Cancel`, ``result.error`` (and possibly ``result.timeout``)
        are set. Callers should inspect those fields to decide success/
        failure — typically by funneling the result through
        :meth:`_handle_result` / :meth:`_process_result`.
        """
        cmd_str, popen_arg, popen_kwargs, input_bytes = self._popen_args(
            cmd, shell, env, cwd, input, keeplines
        )
        if echo is None:
            echo = task.inputs.get("echo", cast("ConfigTask", task).verbose > -1)
        poll_interval = task.inputs.get("poll", 0)
        task_timeout = task.configSpec.timeout
        deadline = (time.monotonic() + task_timeout) if task_timeout else 0.0

        task.logger.verbose("starting background shell: %s", cmd_str)
        proc = subprocess.Popen(popen_arg, **popen_kwargs)

        if input_bytes is not None:
            assert proc.stdin is not None
            proc.stdin.write(input_bytes)
            proc.stdin.close()

        reader = (
            _BackgroundReader(
                proc,
                stdout_filter=stdout_filter,
                stderr_filter=stderr_filter,
            )
            if echo
            else None
        )
        try:
            initial_sleep = task.inputs.get("initial_sleep", 0)
            if initial_sleep:
                if task_timeout:
                    initial_sleep = min(initial_sleep, task_timeout)
                try:
                    proc.wait(timeout=initial_sleep)
                except subprocess.TimeoutExpired:
                    pass
                if reader is not None:
                    reader.pump()

            if proc.poll() is not None:
                return self._collect_background_result(proc, cmd_str, reader)

            while True:
                if reader is not None:
                    reader.pump()

                if deadline and time.monotonic() >= deadline:
                    _terminate_process(proc)
                    task.logger.debug(
                        "Background shell timed out after %s seconds: %s",
                        task_timeout,
                        cmd_str,
                    )
                    result = self._collect_background_result(proc, cmd_str, reader)
                    result.timeout = task_timeout
                    return result

                signal = yield task.suspend(pause=poll_interval)
                if isinstance(signal, Cancel):
                    _terminate_process(proc)
                    task.logger.debug("Background shell cancelled: %s", signal.reason)
                    result = self._collect_background_result(proc, cmd_str, reader)
                    result.error = signal
                    if signal.timeout:
                        result.timeout = signal.timeout
                    return result

                if proc.poll() is not None:
                    return self._collect_background_result(proc, cmd_str, reader)
                task.logger.trace("Background shell still running: %s", cmd_str)
        finally:
            if reader is not None:
                reader.close()

    @classmethod
    def _collect_background_result(
        cls,
        proc: subprocess.Popen,
        cmd_str: str,
        reader: Optional["_BackgroundReader"] = None,
    ):
        if reader is None:
            stdout, stderr = proc.communicate()
        else:
            reader.drain()
            stdout, stderr = reader.stdout(), reader.stderr()
        return cls._finalize_result(
            types.SimpleNamespace(
                stdout=stdout, stderr=stderr, returncode=proc.returncode
            ),
            cmd_str,
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
