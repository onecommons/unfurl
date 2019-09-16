import codecs
from .configurator import Configurator #, Status
from .ansibleconfigurator import AnsibleConfigurator
import json
from ansible.module_utils.k8s.common import K8sAnsibleMixin

class ClusterConfigurator(Configurator):

  @staticmethod
  def _getHost(connectionConfig):
    client = K8sAnsibleMixin().get_api_client(**connectionConfig)
    return client.configuration.host

  def run(self, task):
    # just test the connection
    task.target.attributes['apiServer'] = self._getHost(task.inputs.get('connection', {}))
    yield task.createResult(True, False, "ok")

class ResourceConfigurator(AnsibleConfigurator):
  def dryRun(self, task):
    # print("generating playbook")
    #print(self.findPlaybook(task))
    # print(json.dumps(self.findPlaybook(task), indent=4))
    yield task.createResult(False, False)

  def makeSecret(self, data):
    # XXX omit data from status
    return dict(type='Opaque', apiVersion='v1', kind='Secret',
        data={k: codecs.encode(v.encode(), 'base64').decode() for k, v in data.items()})

  def getDefinition(self, task):
    if task.target.template.isCompatibleType('giterop.nodes.K8sNamespace'):
      return dict(apiVersion='v1', kind='Namespace')
    elif task.target.template.isCompatibleType('giterop.nodes.k8sSecretResource'):
      return self.makeSecret(task.target.attributes.get('data', {}))
    else:
      # if string: parse
      return task.target.attributes.get('definition', {})

  def updateMetadata(self, definition, task):
    namespace = None
    if task.target.parent.template.isCompatibleType('giterop.nodes.K8sNamespace'):
      namespace = task.target.parent.attributes['name']
    md = definition.setdefault('metadata', {})
    if namespace and 'namespace' not in md:
      md['namespace'] = namespace
    #else: error if namespace mismatch?

    # XXX if using target.name, convert into kube friendly dns-style name
    name = task.target.attributes.get('name', task.target.name)
    if 'name' in md and md['name'] != name:
      task.target.attributes['name'] = md['name']
    else:
      md['name'] = name

  def findPlaybook(self, task):
    definition = self.getDefinition(task)
    self.updateMetadata(definition, task)
    state = 'absent' if task.configSpec.action == 'delete' else 'present'
    connection = task.inputs.get('connection') or {}
    moduleSpec = dict(state=state, definition=definition, **connection)
    # print('moduleSpec', moduleSpec)
    return [dict(k8s=moduleSpec)]

  def _processResult(self, task, result):
    task.target.attributes['apiResource'] = result.get('result')

  def getResultKeys(self, task, results):
    # save first time even if it hasn't changed
    return ['result'] # also "method", "diff", invocation
