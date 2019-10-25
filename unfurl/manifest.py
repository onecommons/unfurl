import collections
import os.path
import hashlib
import json
from ruamel.yaml.comments import CommentedMap
from .tosca import ToscaSpec, TOSCA_VERSION

from .support import ResourceChanges, AttributeManager, Status, Priority, Action, Defaults
from .runtime import OperationalInstance, Resource, Capability, Relationship
from .util import UnfurlError, toEnum
from .configurator import Dependency, ConfigurationSpec
from .repo import RevisionManager, findGitRepo
from .yamlloader import YamlConfig, loadFromRepo
from .job import ConfigChange

import logging
logger = logging.getLogger('unfurl')

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
  def __init__(self, spec, path, localEnv=None):
    super(Manifest, self).__init__()
    self.localEnv = localEnv
    self.repo = localEnv and localEnv.instanceRepo
    self.currentCommitId = self.repo and self.repo.revision
    self.tosca = self.loadSpec(spec, path)
    self.specDigest = self.getSpecDigest(spec)
    self.revisions = RevisionManager(self)
    self.path = path

  def loadSpec(self, spec, path):
    if 'tosca' in spec:
      toscaDef = spec['tosca']
    elif 'node_templates' in spec:
      # allow node_templates shortcut
      toscaDef = {'node_templates': spec['node_templates']}
    else:
      toscaDef = {}
    if "node_templates" in toscaDef:
      # shortcut
      toscaDef = dict(tosca_definitions_version=TOSCA_VERSION,
                  topology_template=toscaDef)
    else:
      # make sure this is present
      toscaDef['tosca_definitions_version']=TOSCA_VERSION

    # hack so we can sneak through manifest to the yamlloader
    toscaDef = CommentedMap(toscaDef.items())
    toscaDef.manifest = self
    return ToscaSpec(toscaDef, spec.get('inputs'), spec, path)

  def getSpecDigest(self, spec):
    m = hashlib.sha1() # use same digest function as git
    t = self.tosca.template
    for tpl in [spec, t.topology_template.custom_defs, t.nested_tosca_tpls_with_topology]:
      m.update(json.dumps(tpl, sort_keys=True).encode("utf-8"))
    return m.hexdigest()

  def _ready(self, rootResource, lastChangeId=0):
    self.rootResource = rootResource
    if rootResource:
      rootResource.attributeManager = self
    self.lastChangeId = lastChangeId

  def getRootResource(self):
    return self.rootResource

  def getBaseDir(self):
    return '.'

  def saveJob(self, job):
    pass

  def loadTemplate(self, name, lastChange=None):
    if lastChange:
      return self.revisions.getRevision(lastChange).tosca.getTemplate(name)
    else:
      return self.tosca.getTemplate(name)

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
      raise UnfurlError("can not find changeset for changeid %s" % changeId)

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
          inputs=spec.get('inputs'), inputSchema=spec.get('inputSchema'),
          preConditions=spec.get('preConditions'), postConditions=spec.get('postConditions'))

  def loadResource(self, rname, resourceSpec, parent=None):
    # if parent property is set it overrides the parent argument
    pname = resourceSpec.get('parent')
    if pname:
      parent = self.getRootResource().findResource(pname)
      if parent is None:
        raise UnfurlError('can not find parent resource %s' % pname)

    resource = self._createNodeInstance(Resource, rname, resourceSpec, parent)
    return resource

  def _getLastChange(self, operational):
    if not operational.lastConfigChange:
      return None
    changerecord = self.changeSets[operational.lastConfigChange]
    parentId = changerecord.get('parentId')
    if parentId:
      return self.changeSets[parentId]
    return changerecord

  def _createNodeInstance(self, ctor, name, status, parent):
    operational = self.loadStatus(status)
    templateName = status.get('template', name)
    template = self.loadTemplate(templateName)
    if template is None:
      if operational.lastConfigChange:
        changerecord = self._getLastChange(operational)
        template = self.loadTemplate(templateName, changerecord)
    if template is None:
      raise UnfurlError('missing resource template %s' % templateName)
    logger.debug('template %s: %s', templateName, template)

    resource = ctor(name, status.get('attributes'), parent, template, operational)

    for key, val in status.get('capabilities', {}).items():
      self._createNodeInstance(Capability, key, val, resource)

    for key, val in status.get('requirements', {}).items():
      self._createNodeInstance(Relationship, key, val, resource)

    for key, val in status.get('resources', {}).items():
      self._createNodeInstance(Resource, key, val, resource)

    return resource

  def findRepoFromGitUrl(self, path, isFile=True, importLoader=None, willUse=False):
    repoURL, filePath, revision = findGitRepo(path, isFile, importLoader)
    if not repoURL or not self.localEnv:
      return None, None, None, None
    explicitRevision = revision
    basePath = importLoader.path #XXX check if dir or not
    # if not explicitRevision:
    #   revision = self.repoStatus.get(repoURL)
    repo, filePath, revision, bare = self.localEnv.findOrCreateWorkingDir(repoURL, isFile, revision, basePath)
    # if willUse and (not explicitRevision or not bare):
    #   self.updateRepoStatus(repo, revision)
    return repo, filePath, revision, bare

  def findPathInRepos(self, path, importLoader=None, willUse=False):
    """
    Check if the file path is inside a folder that is managed by a repository.
    If the revision is pinned and doesn't match the repo, it might be bare
    """
    candidate = None
    if self.repo: # our own repo gets first crack
      filePath, revision, bare = self.repo.findPath(path, importLoader)
      if filePath:
        if not bare:
          return self.repo, filePath, revision, bare
        else:
          candidate = (self.repo, filePath, revision, bare)
    if self.localEnv:
      repo, filePath, revision, bare = self.localEnv.findPathInRepos(path, importLoader)
      if repo:
        if bare and candidate:
          return candidate
        else:
          # if willUse:
          #     self.updateRepoStatus(repo, revision)
          return repo, filePath, revision, bare
    return None, None, None, None

  # def updateRepoStatus(self, repos):
  #   self.repoStatus.update({repo['url'] : repo['commit']
  #       for repo in repos.values() if repo.get('commit')})

  def loadHook(self, yamlConfig, templatePath, baseDir):
    #self.updateRepoStatus(yamlConfig.config.get('status', {}).get('repositories',{}))
    repositories = yamlConfig.config.get('spec', {}).get('tosca', {}).get('repositories', {})

    if isinstance(templatePath, dict):
      templatePath = templatePath.copy()
      path = templatePath['file']
      name = os.path.basename(path)
      repo = templatePath.get('repository')
      if isinstance(repo, dict):
        # a full repository spec maybe part of the include
        reponame = repo.pop('name', name)
        # replace spec with just its name
        templatePath['repository'] = reponame
        repositories[reponame] = repo
    else:
      name = os.path.basename(templatePath)
      templatePath = dict(file = templatePath)

    return loadFromRepo(name, templatePath, baseDir, repositories, self)

class SnapShotManifest(Manifest):
  def __init__(self, manifest, commitId):
    self.commitId = commitId
    oldManifest = manifest.repo.show(manifest.path, commitId)
    self.repo = manifest.repo
    self.localEnv =  manifest.localEnv
    self.manifest = YamlConfig(oldManifest, manifest.path,
                                    loadHook=self.loadHook)
    expanded = self.manifest.expanded
    spec = expanded.get('spec', {})
    super(SnapShotManifest, self).__init__(spec, self.manifest.path, self.localEnv)
    # just needs the spec, not root resource
    self._ready(None)
