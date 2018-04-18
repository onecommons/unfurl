import os
from .util import *
from .configurator import *

class AnsibleConfigurator(Configurator):

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
    resource.update(metadata, spec)
    resource.updateResource(changedResource)
    resource.addResource(newresource)
    return status

ConfiguratorTypes.append(AnsibleConfigurator)

def _runPlaybook(playbook, _inventory):
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
  pbex = PlaybookExecutor(playbooks=[playbook], inventory=inventory,
          variable_manager=variable_manager, loader=loader,
          options=options,
          passwords={})
  results = pbex.run()

ANSIBLE_PLAYBOOK_CMD = "ansible-playbook"
AnsiblePath = os.path.join(os.path.dirname(__file__ ) + 'ansible' )

def localAnsible(*paths):
  return os.path.join(AnsiblePath, *paths)

def _parse_ansible_output(result):
  '''
  Extract json from ansible output and convert to expected component result
  '''
  return {}

def _runPlaybook(playbook, params, inventory=None):
  inventoryarg = ["-i", inventory] if inventory else []
  with tempfile.NamedTemporaryFile() as fp:
    json.dump(params or {}, fp)
    return subprocess.check_output([ANSIBLE_PLAYBOOK_CMD, '-e', '@' + fp.name] + inventoryarg)

# class ComponentType(object):
#   def __init__(self, component, cluster, connections):
#     pass #cluster has connection,
#
# class AnsiblePlaybookComponent(ComponentType):
#   @classmethod
#   def isComponentType(component):
#     if component.type == 'ansible':
#       return True
#
#   def __init__(self, component):
#     self.component = component
#
#   def run(self, action, params):
#     #XXX what to do about action? (provision, deprovision, update)
#     return _parse_ansible_output(_runPlaybook(self.component.playbook, self.component.getParams(params)))
#
# ComponentTypes.append(AnsiblePlaybookComponent)
