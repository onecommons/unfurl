import os
from .util import *
from .configurator import *

class AnsibleConfigurator(Configurator):

  #could have parameter for mapping resource attributes to groups
  #also need to map attributes to host vars
  def getInventory(self, resource, parameters):
    # find hosts and how to connect to them
    pass

  #use requires to map attributes to vars,
  #have a parameter for the mapping or just set_facts in playbook
  def getVars(self, resource, parameters):
    pass

  # does this need to be run on this?
  # returns yes, no, unsupported state
  def shouldRun(self, action, resource, parameters):
    configuratorRunStatus = resource.getLastStatus()
    #was run before? if no, do can we handle requested action?
    #if yes, has version or config changed?
    #  if yes, can we update?
    #  if no, did it succeed before?
    return False

  def run(self, action, resource, parameters):
    #build host inventory from resource
    #build vars from parameters
    #map output to resource updates
    #https://github.com/ansible/ansible/blob/devel/lib/ansible/plugins/callback/log_plays.py
    configuratorRunStatus = resource.getLastStatus()
    results = _runPlaybooks([playbook], self.getInventory(resource, parameters), self.getVars(resource, parameters))
    #extract provides from results
    resource.update(metadata, spec)
    resource.updateResource(changedResource)
    resource.addResource(newresource)
    return status

# class ResultCallback(CallbackBase):
#   def v2_runner_on_ok(self, result, **kwargs):
#       host = result._host
#       print json.dumps({host.name: result._result}, indent=4)
#
#   def v2_runner_on_failed(self, result, **kwargs):
#       host = result._host
#       print json.dumps({host.name: result._result}, indent=4)
#
#   def v2_runner_on_unreachable(self, result, **kwargs):
#       host = result._host
#       print json.dumps({host.name: result._result}, indent=4)

ConfiguratorTypes.append(AnsibleConfigurator)

def _runPlaybooks(playbooks, _inventory, params):
  from collections import namedtuple
  from ansible.parsing.dataloader import DataLoader
  from ansible.inventory.manager import InventoryManager
  from ansible.vars.manager import VariableManager
  from ansible.executor.playbook_executor import PlaybookExecutor
  #https://github.com/ansible/ansible/blob/devel/lib/ansible/cli/playbook.py

  loader = DataLoader()
  # create the inventory, and filter it based on the subset specified (if any)
  inventory = InventoryManager(loader=loader, sources=_inventory)
  # create the variable manager, which will be shared throughout
  # the code, ensuring a consistent view of global variables
  variable_manager = VariableManager(loader=loader, inventory=inventory)
  variable_manager.extra_vars = params

  # needed by TaskQueueManager
  Options = namedtuple('Options', ['connection', 'module_path', 'forks', 'become', 'become_method', 'become_user', 'check', 'diff'])
  options = Options(
            connection='smart',
            module_path=module_path, forks=100,
            become=None, become_method=None, become_user=user,
            check=False,
            diff=False
        )
  options.listhosts = options.listtasks = options.listtags = options.syntax = False
  #create the playbook executor, which manages running the plays via a task queue manager
  pbex = PlaybookExecutor(playbooks=playbooks, inventory=inventory,
          variable_manager=variable_manager, loader=loader,
          options=options,
          passwords={})
  results = pbex.run()
  return results

ANSIBLE_PLAYBOOK_CMD = "ansible-playbook"

def _runPlaybook(playbook, params, inventory=None):
  inventoryarg = ["-i", inventory] if inventory else []
  with tempfile.NamedTemporaryFile() as fp:
    json.dump(params or {}, fp)
    return subprocess.check_output([ANSIBLE_PLAYBOOK_CMD, '-e', '@' + fp.name] + inventoryarg)
