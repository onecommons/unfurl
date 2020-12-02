# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
import collections
import os.path
import hashlib
import json
from ruamel.yaml.comments import CommentedMap
from .tosca import ToscaSpec, TOSCA_VERSION, Artifact

from .support import ResourceChanges, AttributeManager, Status, Priority, NodeState
from .runtime import (
    OperationalInstance,
    NodeInstance,
    CapabilityInstance,
    RelationshipInstance,
)
from .util import UnfurlError, toEnum, sensitive_str, getBaseDir
from .repo import RevisionManager, splitGitUrl, isURLorGitPath, Repository
from .yamlloader import YamlConfig, yaml, ImportResolver
from .job import ConfigChange
import toscaparser.imports
import toscaparser.repositories

# from .configurator import Dependency
import logging

logger = logging.getLogger("unfurl")

_basepath = os.path.abspath(os.path.dirname(__file__))


class Manifest(AttributeManager):
    """
    Loads a model from dictionary representing the manifest
    """

    rootResource = None

    def __init__(self, path, localEnv=None):
        super(Manifest, self).__init__(yaml)
        self.localEnv = localEnv
        self.path = path
        self.repo = self._findRepo()
        self.currentCommitId = self.repo and self.repo.revision
        self.revisions = RevisionManager(self)
        self.changeSets = None
        self.tosca = None
        self.specDigest = None
        self.repositories = {}

    def _setSpec(self, config):
        repositories = self.updateRepositories(config, None)
        spec = config.get("spec", {})
        self.tosca = self._loadSpec(spec, self.path, repositories)
        self.specDigest = self.getSpecDigest(spec)

    def _findRepo(self):
        # check if this path exists in the repo
        repo = self.localEnv and self.localEnv.instanceRepo
        if repo:
            path = repo.findPath(self.path)[0]
            if path and (path, 0) in repo.repo.index.entries:
                return repo
        return None

    def _loadSpec(self, spec, path, repositories):
        if "service_template" in spec:
            toscaDef = spec["service_template"]
        elif "tosca" in spec:  # backward compat
            toscaDef = spec["tosca"]
        else:
            toscaDef = {}

        repositoriesTpl = toscaDef.setdefault("repositories", CommentedMap())
        for name, value in repositories.items():
            if name not in repositoriesTpl:
                repositoriesTpl[name] = value

        # make sure this is present
        toscaDef["tosca_definitions_version"] = TOSCA_VERSION

        if not isinstance(toscaDef, CommentedMap):
            toscaDef = CommentedMap(toscaDef.items())
        if getattr(toscaDef, "baseDir", None) and (
            not path or toscaDef.baseDir != os.path.dirname(path)
        ):
            # note: we only recorded the baseDir not the name of the included file
            path = toscaDef.baseDir
        return ToscaSpec(
            toscaDef, spec.get("inputs"), spec, path, self.getImportResolver()
        )

    def getSpecDigest(self, spec):
        m = hashlib.sha1()  # use same digest function as git
        t = self.tosca.template
        for tpl in [spec, t.topology_template.custom_defs, t.nested_tosca_tpls]:
            m.update(json.dumps(tpl, sort_keys=True).encode("utf-8"))
        return m.hexdigest()

    def _ready(self, rootResource):
        self.rootResource = rootResource
        if rootResource:
            rootResource.attributeManager = self

    def getRootResource(self):
        return self.rootResource

    def getBaseDir(self):
        return "."

    @property
    def loader(self):
        return (
            self.rootResource
            and self.rootResource.templar
            and self.rootResource.templar._loader
        )

    def saveJob(self, job):
        pass

    def loadTemplate(self, name, lastChange=None):
        if lastChange:
            try:
                return self.revisions.getRevision(lastChange).tosca.getTemplate(name)
            except:
                return None
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

        instance._priority = toEnum(Priority, status.get("priority"))
        instance._lastStateChange = status.get("lastStateChange")
        instance._lastConfigChange = status.get("lastConfigChange")

        readyState = status.get("readyState")
        if not isinstance(readyState, collections.Mapping):
            instance._localStatus = toEnum(Status, readyState)
        else:
            instance._localStatus = toEnum(Status, readyState.get("local"))
            instance._state = toEnum(NodeState, readyState.get("state"))

        return instance

    @staticmethod
    def loadResourceChanges(changes):
        resourceChanges = ResourceChanges()
        if changes:
            for k, change in changes.items():
                status = change.pop(".status", None)
                resourceChanges[k] = [
                    None if status is None else Manifest.loadStatus(status).localStatus,
                    change.pop(".added", None),
                    change,
                ]
        return resourceChanges

    # XXX unused
    # def loadConfigChange(self, changeId):
    #     """
    # Reconstruct the Configuration that was applied in the past
    # """
    #     changeSet = self.changeSets.get(changeId)
    #     if not changeSet:
    #         raise UnfurlError("can not find changeset for changeid %s" % changeId)
    #
    #     configChange = ConfigChange()
    #     Manifest.loadStatus(changeSet, configChange)
    #     configChange.changeId = changeSet.get("changeId", 0)
    #     configChange.parentId = changeSet.get("parentId")
    #
    #     configChange.inputs = changeSet.get("inputs")
    #
    #     configChange.dependencies = {}
    #     for val in changeSet.get("dependencies", []):
    #         key = val.get("name") or val["ref"]
    #         assert key not in configChange.dependencies
    #         configChange.dependencies[key] = Dependency(
    #             val["ref"],
    #             val.get("expected"),
    #             val.get("schema"),
    #             val.get("name"),
    #             val.get("required"),
    #             val.get("wantList", False),
    #         )
    #
    #     if "changes" in changeSet:
    #         configChange.resourceChanges = self.loadResourceChanges(
    #             changeSet["changes"]
    #         )
    #
    #     configChange.result = changeSet.get("result")
    #     configChange.messages = changeSet.get("messages", [])
    #
    #     # XXX
    #     # ('action', ''),
    #     # ('target', ''), # nodeinstance key
    #     # implementationType: configurator resource | artifact | configurator class
    #     # implementation: repo:key#commitid | className:version
    #     return configChange

    # XXX this is only used by commented out Taskview.createConfigurationSpec()
    # def loadConfigSpec(self, configName, spec):
    #  # XXX need to update configurationSpec in json schema:
    #  #"workflow": { "type": "string" },
    #  #"inputs": { "$ref": "#/definitions/attributes", "default": {} },
    #  #"preconditions": { "$ref": "#/definitions/schema", "default": {} }
    #     return ConfigurationSpec(
    #         configName,
    #         spec["operation"],
    #         spec["className"],
    #         spec.get("majorVersion"),
    #         spec.get("minorVersion", ""),
    #         workflow=spec.get("workflow", Defaults.workflow),
    #         inputs=spec.get("inputs"),
    #         inputSchema=spec.get("inputSchema"),
    #         preConditions=spec.get("preConditions"),
    #         postConditions=spec.get("postConditions"),
    #     )

    def createNodeInstance(self, rname, resourceSpec, parent=None):
        # if parent property is set it overrides the parent argument
        pname = resourceSpec.get("parent")
        if pname:
            parent = self.getRootResource().findResource(pname)
            if parent is None:
                raise UnfurlError("can not find parent instance %s" % pname)

        resource = self._createEntityInstance(NodeInstance, rname, resourceSpec, parent)

        resource._capabilities = []
        for key, val in resourceSpec.get("capabilities", {}).items():
            self._createEntityInstance(CapabilityInstance, key, val, resource)

        resource._requirements = []
        for key, val in resourceSpec.get("requirements", {}).items():
            # parent will be the capability, should have already been created
            capabilityId = val.get("capability")
            if not capabilityId:
                raise UnfurlError("requirement is missing capability %s" % key)
            capability = capabilityId and self.getRootResource().query(capabilityId)
            if not capability or not isinstance(capability, CapabilityInstance):
                raise UnfurlError("can not find capability %s" % capabilityId)
            if capability._relationships is None:
                capability._relationships = []
            requirement = self._createEntityInstance(
                RelationshipInstance, key, val, capability
            )
            requirement.source = resource
            resource.requirements.append(requirement)

        for key, val in resourceSpec.get("instances", {}).items():
            self.createNodeInstance(key, val, resource)

        return resource

    def _getLastChange(self, operational):
        if not operational.lastConfigChange:
            return None
        if not self.changeSets:  # XXX load changesets if None
            return None
        jobId = ConfigChange.getJobId(operational.lastConfigChange)
        return self.changeSets.get(jobId)

    def _createEntityInstance(self, ctor, name, status, parent):
        operational = self.loadStatus(status)
        templateName = status.get("template", name)

        imported = None
        importName = status.get("imported")
        if importName is not None:
            imported = self.imports.findImport(importName)
            if not imported:
                raise UnfurlError("missing import %s" % importName)
            if imported.shadow:
                raise UnfurlError("already imported %s" % importName)
            template = imported.template
        else:
            template = self.loadTemplate(templateName)

        if template is None:
            # not defined in the current model any more, try to retrieve the old version
            if operational.lastConfigChange:
                changerecord = self._getLastChange(operational)
                template = self.loadTemplate(templateName, changerecord)
        if template is None:
            raise UnfurlError("missing resource template %s" % templateName)
        # logger.debug("creating instance for template %s: %s", templateName, template)

        # omit keys that match <<REDACTED>> so can we use the computed property
        attributes = {
            k: v
            for k, v in status.get("attributes", {}).items()
            if v != sensitive_str.redacted_str
        }
        instance = ctor(name, attributes, parent, template, operational)
        if "created" in status:
            instance.created = status["created"]
        if imported:
            self.imports.setShadow(importName, instance)
        return instance

    def findRepoFromGitUrl(self, path, isFile, importLoader):
        repoURL = None
        if not isFile or isURLorGitPath(path):
            # only support urls to git repos for now
            repoURL, filePath, revision = splitGitUrl(path)
        if not repoURL or not self.localEnv:
            return None, None, None, None

        basePath = getBaseDir(importLoader.path)  # checks if dir or not
        repo, revision, bare = self.localEnv.findOrCreateWorkingDir(
            repoURL, revision, basePath
        )
        return repo, filePath, revision, bare

    def findPathInRepos(self, path, importLoader=None):
        """
        Check if the file path is inside a folder that is managed by a repository.
        If the revision is pinned and doesn't match the repo, it might be bare
        """
        if self.localEnv:
            return self.localEnv.findPathInRepos(path, importLoader)
        elif self.repo:
            repo = self.repo
            filePath = repo.findRepoPath(path)
            if filePath is not None:
                return repo, filePath, repo.revision, False
        return None, None, None, None

    def addRepository(self, repo, toscaRepository, file_name):
        repository = self.repositories.get(toscaRepository.name)
        if repository:
            if (
                repository.repo and repository.repo != repo
            ) or repository.repository.tpl != toscaRepository.tpl:
                raise UnfurlError(
                    'Repository "%s" already defined' % toscaRepository.name
                )
            repository = Repository(toscaRepository, repo, file_name)
        self.repositories[repository.name] = repository
        return repository

    def updateRepositories(self, config, inlineRepository):
        repositories = self._getRepositories(config)
        for name, tpl in repositories.items():
            if name not in self.repositories:
                toscaRepository = toscaparser.repositories.Repository(name, tpl)
                self.repositories[name] = Repository(toscaRepository, None)
        if inlineRepository:
            toscaRepository = toscaparser.repositories.Repository(
                inlineRepository.pop("name"), inlineRepository
            )
            self.addRepository(None, toscaRepository, "")
        return self._setRepositories()

    @staticmethod
    def _getRepositories(tpl):
        return ((tpl.get("spec") or {}).get("service_template") or {}).get(
            "repositories"
        ) or {}

    def _setRepositories(self):
        repositories = self.repositories
        if "unfurl" not in repositories:
            # add a repository that points to this package
            repository = Repository(
                toscaparser.repositories.Repository(
                    "unfurl", dict(url="file:" + _basepath)
                ),
                None,
                "",
            )
            repositories["unfurl"] = repository
        if "self" not in repositories:
            # this is called too early to use self.getBaseDir()
            path = getBaseDir(self.path) if self.path else "."
            if self.repo:
                url = self.repo.getGitLocalUrl(path, "self")
                path = os.path.relpath(path, self.repo.workingDir)
            else:
                url = "file:" + path
            repository = Repository(
                toscaparser.repositories.Repository("self", dict(url=url)),
                self.repo,
                path,
            )
            repositories["self"] = repository
        if "spec" not in repositories:
            # if not found assume it points the project root or self if not in a project
            if (
                self.localEnv
                and self.localEnv.project
                and self.localEnv.project is not self.localEnv.homeProject
            ):
                repo = self.localEnv.project.projectRepo
                path = self.localEnv.project.projectRoot
                if repo:
                    assert path
                    url = repo.getGitLocalUrl(path, "spec")
                    path = os.path.relpath(path, repo.workingDir)
                else:
                    url = "file:" + path

                repository = Repository(
                    toscaparser.repositories.Repository("spec", dict(url=url)),
                    self.localEnv.project.projectRepo,
                    path,
                )
                repositories["spec"] = repository
            else:
                repositories["spec"] = repositories["self"]
        return {name: repo.repository.tpl for name, repo in repositories.items()}

    def loadYamlInclude(
        self, yamlConfig, templatePath, baseDir, warnWhenNotFound=False
    ):
        """
        This is called while the YAML config is being loaded.
        Returns (url or fullpath, parsed yaml)
        """

        inlineRepository = None
        if isinstance(templatePath, dict):
            artifactTpl = templatePath.copy()
            path = artifactTpl["file"]
            repo = artifactTpl.get("repository")
            if isinstance(repo, dict):
                # a full repository spec maybe part of the include
                reponame = repo.setdefault("name", os.path.basename(path))
                # replace spec with just its name
                artifactTpl["repository"] = reponame
                inlineRepository = repo
            artifact = Artifact(artifactTpl, path=baseDir)
        else:
            artifact = Artifact(dict(file=templatePath), path=baseDir)

        tpl = CommentedMap()
        tpl["repositories"] = self.updateRepositories(
            yamlConfig.config, inlineRepository
        )
        loader = toscaparser.imports.ImportsLoader(
            None,
            artifact.baseDir,
            tpl=tpl,
            resolver=self.getImportResolver(warnWhenNotFound),
        )
        return loader._load_import_template(None, artifact.asImportSpec())

    def statusSummary(self):
        def summary(instance, indent):
            print(" " * indent, instance)
            indent += 4
            for child in instance.instances:
                summary(child, indent)

        summary(self.rootResource, 0)

    def getImportResolver(self, ignoreFileNotFound=False):
        return ImportResolver(self, ignoreFileNotFound)


class SnapShotManifest(Manifest):
    def __init__(self, manifest, commitId):
        super(SnapShotManifest, self).__init__(manifest.path, self.localEnv)
        self.commitId = commitId
        oldManifest = manifest.repo.show(manifest.path, commitId)
        self.repo = manifest.repo
        self.localEnv = manifest.localEnv
        self.manifest = YamlConfig(
            oldManifest, manifest.path, loadHook=self.loadYamlInclude
        )
        expanded = self.manifest.expanded
        # just needs the spec, not root resource
        self._setSpec(expanded)
        self._ready(None)
