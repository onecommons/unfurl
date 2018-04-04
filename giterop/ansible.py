import os
from .util import *

#use easy_ansible python package instead of launching ansible-playbook?
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

class AnsiblePlaybookComponent(object):
  @classmethod
  def isComponentType(component):
    if component.type == 'ansible':
      return True

  def __init__(self, component):
    self.component = component

  def run(self, action, params):
    #XXX what to do about action? (provision, deprovision, update)
    return _parse_ansible_output(_runPlaybook(self.component.playbook, self.component.getParams(params)))

ComponentTypes.append(AnsiblePlaybookComponent)
