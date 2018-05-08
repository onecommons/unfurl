import os
import tempfile
import json
import collections

from .util import *
from .configurator import *

import ansible.constants as C
from ansible.cli.playbook import PlaybookCLI
from ansible.plugins.callback import CallbackBase

class AnsibleConfigurator(Configurator):

  def __init__(self, configuratorDef):
    super(AnsibleConfigurator, self).__init__(configuratorDef)
    self._cleanupRoutines = []

  def getCustomProperties(self):
    return {}

  #could have parameter for mapping resource attributes to groups
  #also need to map attributes to host vars
  def getInventory(self, job):
    # find hosts and how to connect to them
    return 'localhost,'
    # tp = tempfile.NamedTemporaryFile(suffix='.json')
    # self._cleanupRoutines.append(lambda: tp.close())
    # json.dump({"localhost"}, fp)
    # return tp.name

  def _cleanup(self):
    for func in self._cleanupRoutines:
      try:
        func()
      except:
        # XXX: log
        pass
    self._cleanupRoutines = []

  #use requires to map attributes to vars,
  #have a parameter for the mapping or just set_facts in playbook
  def getVars(self, job):
    pass

  def run(self, job):
    #build host inventory from resource
    #build vars from parameters
    #map output to resource updates
    #https://github.com/ansible/ansible/blob/devel/lib/ansible/plugins/callback/log_plays.py
    try:
      inventory = self.getInventory(job)
      results = runPlaybooks([playbook], inventory, self.getVars(job))
      host = results.inventoryManager.get_host(hostname)
      host_vars = results.variableManager.get_vars(host=host)
      #extract provides from results
      job.resource.update()
      job.createResource()
      job.discoverResource()
    finally:
      self.cleanup()

    return status

registerClass(VERSION, "Ansible", AnsibleConfigurator)

#https://github.com/ansible/ansible/blob/d72587084b4c43746cdb13abb262acf920079865/examples/scripts/uptime.py
_ResultsByStatus = collections.namedtuple('_ResultsByStatus', "ok failed skipped unreachable")
class ResultCallback(CallbackBase):
  #see ansible.executor.task_result.TaskResult and ansible.playbook.task.Task

  def __init__(self):
    self.results = []
    self.resultsByStatus = _ResultsByStatus(*[collections.OrderedDict() for x in range(4)])

  def getInfo(self, result):
    host = result._host
    taskname = result.task_name
    fields = result._task_fields.keys()
    keys = result._result.keys()
    return "%s: %s(%s) => %s" % (host, taskname, fields, keys)

  def _addResult(self, status, result):
    self.results.append(result)
    getattr(self.resultsByStatus, status)[result.task_name] = result

  def v2_runner_on_ok(self, result, **kwargs):
    self._addResult('ok', result)
    #print("ok", self.getInfo(result))

  def v2_runner_on_skipped(self, result):
    self._addResult('skipped', result)
    #print("skipped", self.getInfo(result))

  def v2_runner_on_failed(self, result, **kwargs):
    self._addResult('failed', result)
    #print("failed", self.getInfo(result))

  def v2_runner_on_unreachable(self, result, **kwargs):
    self._addResult('unreachable', result)
    #print("unreachable", self.getInfo(result))

def runPlaybooks(playbooks, _inventory, params=None, args=None):
  # util should have set this up already
  args=['ansible-playbook', '-i', _inventory] + (args or []) + playbooks
  cli = PlaybookCLI(args)
  cli.parse()
  ansibleDisplay.verbosity = cli.options.verbosity

  # CallbackBase imports __main__.cli (which is set to ansibleDummyCli)
  # as assigns its options to self._options
  ansibleDummyCli.options.__dict__.update(cli.options.__dict__)

  #C.DEFAULT_STDOUT_CALLBACK == 'default' (ansible/plugins/callback/default.py)
  resultsCB = ResultCallback()
  C.DEFAULT_STDOUT_CALLBACK = resultsCB

  _play_prereqs = cli._play_prereqs
  def hook_play_prereqs(options):
    loader, inventory, variable_manager = _play_prereqs(options)
    if params:
      variable_manager._extra_vars.update(params)
    resultsCB.inventoryManager = inventory
    resultsCB.variableManager = variable_manager
    return loader, inventory, variable_manager
  cli._play_prereqs = hook_play_prereqs

  resultsCB.exit_code = cli.run()
  return resultsCB
