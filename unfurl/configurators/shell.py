"""
inputs:
 command: "--switch {{ '.::foo' | ref }}"
 timeout: 9999
 resultTemplate: # cmd, stdout, stderr
   ref:
    file:
      ./handleResult.tpl
  foreach: contents
"""


# see also 13.4.1 Shell scripts p 360
# XXX add support for a stdin parameter

from ..configurator import Status
from . import TemplateConfigurator
import os

# import os.path
import sys
import six

if os.name == "posix" and sys.version_info[0] < 3:
    import subprocess32 as subprocess
else:
    import subprocess
# cf https://github.com/opsmop/opsmop/blob/master/opsmop/core/command.py

import logging

logger = logging.getLogger("unfurl")

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
    def runProcess(self, cmd, shell=False, timeout=None, env=None):
        """
    Returns an object with the following attributes:

    cmd
    timeout (None unless timeout occurred)
    stderr
    stdout
    returncode (None if the process didn't complete)
    error if an exception was raised
    """
        if not isinstance(cmd, six.string_types):
            cmdStr = " ".join(cmd)
        else:
            cmdStr = cmd

        try:
            completed = subprocess.run(
                cmd,
                shell=shell,
                env=env,
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
            logger.warning("shell task run failure: %s", result.cmd)
        else:
            logger.info("shell task run success: %s", result.cmd)

        if status != Status.error:
            self.processResultTemplate(task, result.__dict__)
            if task.errors:
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

    def run(self, task):
        params = task.inputs
        cmd = params["command"]
        # default for shell: True if command is a string otherwise False
        shell = params.get("shell", isinstance(cmd, six.string_types))
        env = task.getEnvironment(False)
        result = self.runProcess(
            cmd, shell=shell, timeout=task.configSpec.timeout, env=env
        )
        status = self._handleResult(task, result)
        yield task.done(status == Status.ok, status=status, result=result.__dict__)
