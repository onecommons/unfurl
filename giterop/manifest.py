import collections
import os.path
from ruamel.yaml.comments import CommentedMap

from .support import ResourceChanges, AttributeManager, Status, Priority, Action, Defaults
from .runtime import OperationalInstance, Resource, Capability, Relationship, ConfigChange
from .util import GitErOpError, toEnum
from .configurator import Dependency, ConfigurationSpec

import logging
logger = logging.getLogger('giterop')

class Repo(object):
  @staticmethod
  def makeRepo(url, repotype, basedir):
    # XXX git or simple based on url
    # basedir is the project/subproject root or local-config root dependening where the import definition lives
    if repotype=='instance':
      dir = os.path.join(basedir, 'instances', 'current')
    else:
      dir = os.path.join(basedir, repotype)
    # XXX error if exists, else mkdirs
    return SimpleRepo(url, dir)

  def checkout(self, commitid):
    return workingDir

  def commit(self):
    return commitid

class SimpleRepo(Repo):
  def __init__(self, lastCommitId):
    self.lastCommitId = int(lastCommitId or 0)

  def checkout(self, commitid, useCurrent):
    if useCurrent and commitid == self.lastCommitId:
      return self.workingDir
    return './revisions/{commitid}/files'

  def commit(self):
    self.lastCommitId += 1
    # copy current workingDir to /revisions/{commitid}/files
    return self.lastCommitId

class Revision(object):
  def __init__(self, revisionManager, commitid, workingDir):
    self.revisionManager = revisionManager
    self.workingDir = workingDir
    self.commitId = commitid

class RevisionManager(object):
  def __init__(self, currentToscaTemplate, localEnv=None):
    self.revisions = {}
    self.currentToscaTemplate = currentToscaTemplate
    self.localEnv = localEnv

  def getRevision(self, repo, commitid):
    key = (repo, commitid)
    if key in self.revisions:
      return self.revisions[key]
    else:
      workingDir = '.' # XXX self.repos[repo].checkout(commitid)
      revision = Revision(self, commitid, workingDir)
      revision.template = self.currentToscaTemplate
      self.revisions[key] = revision
      return revision

  def getRepoWorkingDir(self, uri, commitid=None):
    localEnv = self.localEnv
    if 'uri' not in localEnv:
      repo = Repo.makeRepo(uri)
      localEnv.addRepo(repo)

    return localEnv[uri].checkout(commitid, True)

ChangeRecordAttributes = CommentedMap([
   ('changeId', 0),
   ('parentId', None),
   ('commitId', ''),
   ('startTime', ''),
]);

class Manifest(AttributeManager):
  """
  Loads a model from dictionary representing the manifest
  """
  def __init__(self, currentToscaTemplate):
    super(Manifest, self).__init__()
    self.tosca = currentToscaTemplate
    assert currentToscaTemplate
    self.revisions = RevisionManager(self.tosca)

  def _ready(self, rootResource, lastChangeId=0):
    self.rootResource = rootResource
    rootResource.attributeManager = self
    self.lastChangeId = lastChangeId

  def getRootResource(self):
    return self.rootResource

  def getBaseDir(self):
    return '.'

  def saveJob(self, job):
    pass

  def getTemplateUri(self, template):
    return 'self:%s:0' % template.name

  def loadTemplate(self, template):
    if ':' in template:
      repo, name, commitId = template.split(':')
      revision = self.revisions.getRevision(repo, commitId)
      tosca = revision.template
    else:
      name = template
      tosca = self.tosca
    return tosca.getTemplate(name)

#  load instances
#    create a resource with the given template
#  or generate a template setting interface with the referenced implementations

  @staticmethod
  def loadStatus(status, instance=None):
    if not instance:
      instance = OperationalInstance()
    if not status:
      return instance

    instance._priority = toEnum(Priority, status.get('priority'))
    instance._lastStateChange = status.get('lastStateChange')
    instance._lastConfigChange = status.get('lastConfigChange')

    readyState = status.get('readyState')
    if not isinstance(readyState, collections.Mapping):
      instance._localStatus = toEnum(Status, readyState)
    else:
      instance._localStatus = toEnum(Status, readyState.get('local'))

    return instance

  @staticmethod
  def loadResourceChanges(changes):
    resourceChanges = ResourceChanges()
    if changes:
      for k, change in changes.items():
        status = change.pop('.status', None)
        resourceChanges[k] = [
          None if status is None else Manifest.loadStatus(status).localStatus,
          change.pop('.added', None),
          change
        ]
    return resourceChanges

  def loadConfigChange(self, changeId):
    """
    Reconstruct the Configuration that was applied in the past
    """
    changeSet = self.changeSets.get(changeId)
    if not changeSet:
      raise GitErOpError("can not find changeset for changeid %s" % changeId)

    configChange = ConfigChange()
    Manifest.loadStatus(changeSet, configChange)
    for (k,v) in ChangeRecordAttributes.items():
      setattr(self, k, changeSet.get(k, v))

    configChange.inputs = changeSet.get('inputs')

    configChange.dependencies = {}
    for val in changeSet.get('dependencies', []):
      key = val.get('name') or val['ref']
      assert key not in configChange.dependencies
      configChange.dependencies[key] = Dependency(val['ref'], val.get('expected'),
        val.get('schema'), val.get('name'), val.get('required'), val.get('wantList', False))

    if 'changes' in changeSet:
      configChange.resourceChanges = self.loadResourceChanges(changeSet['changes'])

    configChange.result = changeSet.get('result')
    configChange.messages = changeSet.get('messages', [])

    # XXX
    # ('action', ''),
    # ('target', ''), # nodeinstance key
    # implementationType: configurator resource | artifact | configurator class
    # implementation: repo:key#commitid | className:version
    return configChange

  # find config spec from potentially old version of the tosca template
  # get template then get node template name
  # but we shouldn't need this, except maybe to revert?
  def loadConfigSpec(self, configName, spec):
    return ConfigurationSpec(configName, spec['action'], spec['className'],
          spec.get('majorVersion'), spec.get('minorVersion',''),
          intent=toEnum(Action, spec.get('intent', Defaults.intent)),
          parameters=spec.get('inputs'), parameterSchema=spec.get('inputsSchema'),
          preConditions=spec.get('preConditions'), postConditions=spec.get('postConditions'))

  def loadResource(self, rname, resourceSpec, parent=None):
    # if parent property is set it overrides the parent argument
    pname = resourceSpec.get('parent')
    if pname:
      parent = self.getRootResource().findResource(pname)
      if parent is None:
        raise GitErOpError('can not find parent resource %s' % pname)

    resource = self._createNodeInstance(Resource, rname, resourceSpec, parent)
    return resource

  def _createNodeInstance(self, ctor, name, status, parent):
    templateName = status.get('template', name)
    template = self.loadTemplate(templateName)
    if template is None:
      raise GitErOpError('missing resource template %s' % templateName)
      logger.debug('template %s: %s', templateName, template)

    operational = self.loadStatus(status)
    resource = ctor(name, status.get('attributes'), parent, template, status=operational.localStatus)
    if status.get('createdOn'):
      changeset = self.changeSets.get(status['createdOn'])
      resource.createdOn = changeset.changeRecord if changeset else None
    resource.createdFrom = status.get('createdFrom')

    for key, val in status.get('capabilities', {}).items():
      self._createNodeInstance(Capability, key, val, resource)

    for key, val in status.get('requirements', {}).items():
      self._createNodeInstance(Relationship, key, val, resource)

    for key, val in status.get('resources', {}).items():
      self._createNodeInstance(Resource, key, val, resource)

    return resource
