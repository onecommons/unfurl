"""
parameters:
 command: "--switch {{ '.::foo' | ref }}"
 timeout: 9999
 resultTemplate:
  # cmd, stdout, stderr
  ref:
    file:
      ./handleResults.tpl
  select: contents
"""

# support tosca 4.2 Environment Variable Conventions (p 153)
# at least expose config parameters
# see also 13.3.1 Shell scripts p 328
# XXX add support for a stdin parameter
# (that's a reason to make config parameters lazy too)

from giterop.configurator import Configurator, Status
import json
import os
#import os.path
import sys
import six
import tempfile
if os.name == 'posix' and sys.version_info[0] < 3:
    import subprocess32 as subprocess
else:
    import subprocess
# cf https://github.com/opsmop/opsmop/blob/master/opsmop/core/command.py


# XXX set environment vars?
class ShellConfigurator(Configurator):

  def runProcess(self, cmd, shell=False, timeout=None):
    """
    Returns an object with the following attributes:

    cmd
    timeout (None if there was no timeout)
    stderr
    stdout
    returncode (None if the process didn't complete)
    error if an exception was raised
    """
    if isinstance(cmd, list):
      cmdStr = " ".cmd.join(cmd)
    else:
      cmdStr = cmd

    try:
      completed = subprocess.run(cmd, shell=shell, timeout=timeout,
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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
      completed.timeout = timeout
      completed.error = None
      return completed
    except subprocess.TimeoutExpired as err:
      err.returncode = None
      err.error = err
      return err
    except Exception as err:
      err.cmd = cmdStr
      err.timeout = None
      err.stderr = None
      err.stdout = None
      err.returncode = None
      err.error = err
      return err

  def handleResults(self, task, params, result):
    status = Status.error if result.error or result.returncode else Status.ok
    if status != Status.error and params.get('resultTemplate'):
      results = task.query({
        'eval': dict(template=params['resultTemplate']),
        'vars': result.__dict__})
      if results and results.strip():
        task.updateResources(results)
    return status

  def run(self, task):
    params = task.configSpec.parameters
    assert self.canRun(task)
    cmd = params['command']
    # default for shell: True if command is a string otherwise False
    shell = params.get('shell', isinstance(cmd, six.string_types))
    result = self.runProcess(cmd, shell=shell, timeout=params.get('timeout'))
    status = self.handleResults(task, params, result)
    yield task.createResult(True, True, status, result=result.__dict__)
