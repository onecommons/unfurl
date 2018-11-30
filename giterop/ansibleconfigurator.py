import os
import tempfile
import json
import collections

from .util import *
from .runtime import *

import ansible.constants as C
from ansible.cli.playbook import PlaybookCLI
from ansible.plugins.callback import CallbackBase

def runTemplate(data, dataLoader=None, vars=None):
  from ansible.template import Templar
  # dataLoader can be None, is only used by _lookup and to set _basedir (else ./)
  # see https://github.com/ansible/ansible/test/units/template/test_templar.py
  return Templar(dataLoader, variables=vars).template(data)

# https://github.com/ansible/ansible-runner
# ansible fails by host
class AnsibleConfigurator(Configurator):
  """
  The current resource is the inventory.
  #could have parameter for mapping resource attributes to groups
  #also need to map attributes to host vars
  sshconfig
  ansible variables can not be set to a value of type resource

  external inventory discovers resources
  need away to map hosts to existing resources
  and to map vars to types of different resource
  """

  def __init__(self, configuratorDef):
    super(AnsibleConfigurator, self).__init__(configuratorDef)
    self._cleanupRoutines = []

  def getCustomProperties(self):
    return {}

  #could have parameter for mapping resource attributes to groups
  #also need to map attributes to host vars
  def getInventory(self, task):
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

  def _makeResource(self, var):
    """
     # find a resource that matches the ansible host
     # or create / discover new resources from the ansible host
     - host:
        match:
          '.:hostname': vf:':hostname'

#declare a ansible var and map it to resource metadata
     - hostvar: var1
       var1: vf:'.'

#map a hostvar to metadata on a particular resource
     - hostvar: var1
       target: vf:':host?' # target resource that matches host?
       metadata:
        var1: vf:'.'

    # create/discover new resource from hostvar
     - hostvar: ec2_service
       resource:
         template: ec2serviceResourceTemplate
         name: vf: ':gename:'
         metadata:
          # . is current var?
          # .. is hosts' hostvars?
          foo: vf: '..:ddd'
    """

  #use requires to map attributes to vars,
  #have a parameter for the mapping or just set_facts in playbook
  def getVars(self, task):
    """
    just add a lookup() plugin?
    add attribute to parameters to map to vars?
    # ansible yaml file but we evaluate the valuesFrom first?
    """
    pass

  def getArgs(self):
    return []

  def run(self, task):
    #build host inventory from resource
    #build vars from parameters
    #map output to resource updates
    #https://github.com/ansible/ansible/blob/devel/lib/ansible/plugins/callback/log_plays.py
    try:
      inventory = self.getInventory(task)
      playbook = self.getPlayBook(task)
      extraVars = self.getVars(task)
      results = runPlaybooks([playbook], inventory, extraVars, self.getArgs())
      hostname = task.getResource().get('hostname')
      host = results.inventoryManager.get_host(hostname)
      host_vars = results.variableManager.get_vars(host=host)
      #extract provides from results
      task.resource.update()
      task.createResource()
      task.discoverResource()
      return self.status.failed if results.exit_code  else self.status.success
    finally:
      self.cleanup()

#https://github.com/ansible/ansible/blob/d72587084b4c43746cdb13abb262acf920079865/examples/scripts/uptime.py
_ResultsByStatus = collections.namedtuple('_ResultsByStatus', "ok failed skipped unreachable")
class ResultCallback(CallbackBase):
  # NOTE: callbacks will run in seperate process
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
    # XXX should save by host too
    getattr(self.resultsByStatus, status).setdefault(result.task_name, []).append(result)

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

  #see also https://github.com/projectatomic/atomic-host-tests/blob/master/callback_plugins/default.py
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

class IncrementalRunner(object):
  """
  Before the play is run, we assign each task a position based on the order it is statically queued.
  After a task is run the position of the task is recorded. It should always be greater than the last position.

  Re-running a playbook with an identical configuration, we can skip queuing tasks whose position is less
  than the last completed for all currently targeted hosts.

  We can save check points with a task names and digests to verify that the save positions correspond to the same task.

  For dynamic includes (invoked by a strategy plugin) we can't rely on the task's invocation order being deterministic.
  """
  def __init__(self):
    self.foo = 1

# monkey patch Task/Taggable.evaluate_tags ?
# which is only called by Block.filter_tagged_tasks
# which in turn is called by Playbook iterator and strategy plugins
# but won't be called per host -- update "self.when" on task?

# TaskQueueManager.run() / PlayIterator() is called for each playbook and for each possible batches of hosts
# (see playbook_executor.py#L151)
# so _Gindex should by playbook at least
# doesn't this assume a task is only executed once? bad assumption!

  #key is iterator.get_next_task_for_host
  # see StrategyBase._queue_task and _process_pending_results
# workers run executor.task_executor

def shouldRunAnsibleTask(self, task, only_tags, skip_tags, all_vars):
  # playbooks haven't changed, inventory and host_vars haven't changed,
  #configuration haven't changed, etc.
  if not self.lastCompatibleRun:
    return True
  #hmmm how to uniquely identity tasks and their order????
  pos = task._Gindex
  if pos < 0:
    return True
  #we ran before successfully don't need to do it again
  return self.lastCompatibleRun.completedTaskIndex < pos

def getAnsibleTaskDigest(task):
  d = task.serialize()
  del d['uuid']
  return hasher(d)

def setTaskPos(self, task):
  #task._parent.__class__.__name__
  #if not isReproducible(task):
  #  return -1

  digest = getAnsibleTaskDigest(task)
  if digest not in self.tasks:
    self.tasks[task._uuid] = task
    task._Gindex = len(self.tasks)
  else:
    assert task._Gindex > -1

def evaluate_tags(task, tags, all_vars):
  if not hasattr(task, '_Gindex'):
    setattr(task, '_Gindex', -1)

  res = super(Task, task).evaluate_tags(self, only_tags, skip_tags, all_vars)
  if not res:
    return res

  if 'always' in self.tags:
    return True

  #all_vars is either all_vars or task_vars when called by strategy
  state = all_vars.get('__giterop')
  if state:
    if state.currentTask:
      state.currentTask.setTaskPos(self)
      return state.currentTask.shouldRunAnsibleTask(self, only_tags, skip_tags, all_vars)
  else:
    task._Gindex = -1
    #isn't being called from a static position
    #don't update task.position and if the task has already set, remove it so we don't confused when it is run
  return res
