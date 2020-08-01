"""
enviroment:
timeout:
inputs:
 command: "--switch {{ '.::foo' | ref }}"
 cwd
 dryrun
 shell
 keeplines
 resultTemplate: # available vars: cmd, stdout, stderr, returncode, error, timeout
   eval:
    file:
      ./handleResult.tpl
   foreach: contents # get the file contents
"""


# see also 13.4.1 Shell scripts p 360
# XXX add support for a stdin parameter

from ..configurator import Status
from . import TemplateConfigurator
import os
import sys
import six

if os.name == "posix" and sys.version_info[0] < 3:
    import subprocess32 as subprocess
else:
    import subprocess
# cf https://github.com/opsmop/opsmop/blob/master/opsmop/core/command.py

try:
    from shutil import which
except ImportError:
    from distutils import spawn

    def which(executable, mode=os.F_OK | os.X_OK, path=None):
        executable = spawn.find_executable(executable, path)
        if executable:
            if os.access(executable, mode):
                return executable
        return None


# XXX we should know if cmd if not os.access(implementation, os.X):
class ShellConfigurator(TemplateConfigurator):
    @staticmethod
    def _cmd(cmd, keeplines):
        if not isinstance(cmd, six.string_types):
            cmdStr = " ".join(cmd)
        else:
            if not keeplines:
                cmd = cmd.replace("\n", " ")
            cmdStr = cmd
        return cmdStr, cmd

    def runProcess(
        self, cmd, shell=False, timeout=None, env=None, cwd=None, keeplines=False
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
            completed = subprocess.run(
                cmd,
                shell=shell,
                env=env,
                cwd=cwd,
                timeout=timeout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
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
            err.returncode = None
            err.error = None
            return err
        except Exception as err:
            err.cmd = cmdStr
            err.timeout = None
            err.stderr = None
            err.stdout = None
            err.returncode = None
            err.error = err
            return err

    def _handleResult(self, task, result):
        status = (
            Status.error
            if result.error or result.returncode or result.timeout
            else Status.ok
        )
        if status == Status.error:
            task.logger.warning("shell task run failure: %s", result.cmd)
            task.logger.info(
                "shell task return code: %s, stderr: %s",
                result.returncode,
                result.stderr,
            )
        else:
            task.logger.info("shell task run success: %s", result.cmd)
            task.logger.debug("shell task output: %s", result.stdout)

        self.processResultTemplate(task, result.__dict__)
        if task._errors:
            return Status.error
        return status

    def canRun(self, task):
        params = task.inputs
        cmd = params.get("command")
        if not cmd:
            return "missing command to execute"
        if isinstance(cmd, list) and not params.get("shell") and not which(cmd[0]):
            return "'%s' is not executable" % cmd[0]
        return True

    def canDryRun(self, task):
        return task.inputs.get("dryrun")

    def run(self, task):
        params = task.inputs
        cmd = params["command"]
        isString = isinstance(cmd, six.string_types)
        # default for shell: True if command is a string otherwise False
        shell = params.get("shell", isString)
        env = task.getEnvironment(False)

        cwd = params.get("cwd")
        if cwd:
            # if cwd is relative, make it relative to task.cwd
            cwd = os.path.abspath(os.path.join(task.cwd, cwd))
        else:
            cwd = task.cwd
        keeplines = params.get("keeplines")

        if task.dryRun and isinstance(task.inputs.get("dryrun"), six.string_types):
            dryrunArg = task.inputs["dryrun"]
            if "%dryrun%" in cmd:  # replace %dryrun%
                if isString:
                    cmd = cmd.replace("%dryrun%", dryrunArg)
                else:
                    cmd[cmd.index("%dryrun%")] = dryrunArg
            else:  # append dryrunArg
                if isString:
                    cmd += " " + dryrunArg
                else:
                    cmd.append(dryrunArg)
        elif "%dryrun%" in cmd:
            if isString:
                cmd = cmd.replace("%dryrun%", "")
            else:
                cmd.remove("%dryrun%")

        result = self.runProcess(
            cmd,
            shell=shell,
            timeout=task.configSpec.timeout,
            env=env,
            cwd=cwd,
            keeplines=keeplines,
        )
        status = self._handleResult(task, result)
        yield task.done(status == Status.ok, result=result.__dict__)
