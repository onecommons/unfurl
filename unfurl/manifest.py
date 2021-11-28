# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
from collections.abc import Mapping
import os.path
import hashlib
import json
from ruamel.yaml.comments import CommentedMap
from .tosca import ToscaSpec, TOSCA_VERSION, ArtifactSpec

from .support import ResourceChanges, AttributeManager, Status, Priority, NodeState
from .runtime import (
    OperationalInstance,
    NodeInstance,
    CapabilityInstance,
    RelationshipInstance,
    ArtifactInstance,
)
from .util import UnfurlError, to_enum, sensitive_str, get_base_dir
from .repo import RevisionManager, split_git_url, RepoView
from .merge import patch_dict
from .yamlloader import YamlConfig, yaml, ImportResolver
from .job import ConfigChange
import toscaparser.imports
from toscaparser.repositories import Repository

# from .configurator import Dependency
import logging

logger = logging.getLogger("unfurl")

_basepath = os.path.abspath(os.path.dirname(__file__))


class Manifest(AttributeManager):
    """
    Base class for managing an ensemble.
    Derived classes handle loading and serialization.
    """

    rootResource = None

    def __init__(self, path, localEnv=None):
        super().__init__(yaml)
        self.localEnv = localEnv
        self.path = path
        self.repo = self._find_repo()
        self.currentCommitId = self.repo and self.repo.revision
        self.revisions = RevisionManager(self)
        self.changeSets = None
        self.tosca = None
        self.specDigest = None
        self.repositories = {}

    def _set_spec(self, config, more_spec=None):
        """
        Set the TOSCA service template.
        """
        repositories = {
            name: repo.repository.tpl for name, repo in self.repositories.items()
        }
        spec = config.get("spec", {})
        self.tosca = self._load_spec(spec, self.path, repositories, more_spec)
        self.specDigest = self.get_spec_digest(spec)

    def _find_repo(self):
        # check if this path exists in the repo
        repo = self.localEnv and self.localEnv.instanceRepo
        if repo:
            path = repo.find_path(self.path)[0]
            if path and (path, 0) in repo.repo.index.entries:
                return repo
        return None

    def _load_spec(self, spec, path, repositories, more_spec):
        if "service_template" in spec:
            toscaDef = spec["service_template"] or {}
        elif "tosca" in spec:  # backward compat
            toscaDef = spec["tosca"] or {}
        else:
            toscaDef = {}

        repositoriesTpl = toscaDef.setdefault("repositories", CommentedMap())
        for name, value in repositories.items():
            if name not in repositoriesTpl:
                repositoriesTpl[name] = value

        # make sure this is present
        toscaDef["tosca_definitions_version"] = TOSCA_VERSION

        if more_spec:
            patch_dict(toscaDef, more_spec, True)

        if not isinstance(toscaDef, CommentedMap):
            toscaDef = CommentedMap(toscaDef.items())
        if getattr(toscaDef, "base_dir", None) and (
            not path or toscaDef.base_dir != os.path.dirname(path)
        ):
            # note: we only recorded the baseDir not the name of the included file
            path = toscaDef.base_dir
        return ToscaSpec(toscaDef, spec, path, self.get_import_resolver(expand=True))

    def get_spec_digest(self, spec):
        m = hashlib.sha1()  # use same digest function as git
        t = self.tosca.template
        for tpl in [spec, t.topology_template.custom_defs, t.nested_tosca_tpls]:
            m.update(json.dumps(tpl, sort_keys=True).encode("utf-8"))
        return m.hexdigest()

    def _ready(self, rootResource):
        """
        Set the instance model.
        """
        self.rootResource = rootResource
        if rootResource:
            rootResource.attributeManager = self

    def get_root_resource(self):
        return self.rootResource

    def get_base_dir(self):
        return "."

    @property
    def loader(self):
        return (
            self.rootResource
            and self.rootResource.templar
            and self.rootResource.templar._loader
        )

    def save_job(self, job):
        pass

    def load_template(self, name, lastChange=None):
        if lastChange:
            try:
                return self.revisions.get_revision(lastChange).tosca.get_template(name)
            except:
                return None
        else:
            return self.tosca.get_template(name)

    #  load instances
    #    create a resource with the given template
    #  or generate a template setting interface with the referenced implementations

    @staticmethod
    def load_status(status, instance=None):
        if not instance:
            instance = OperationalInstance()
        if not status:
            return instance

        instance._priority = to_enum(Priority, status.get("priority"))
        instance._lastStateChange = status.get("lastStateChange")
        instance._lastConfigChange = status.get("lastConfigChange")

        readyState = status.get("readyState")
        if not isinstance(readyState, Mapping):
            instance._localStatus = to_enum(Status, readyState)
        else:
            instance._localStatus = to_enum(Status, readyState.get("local"))
            instance._state = to_enum(NodeState, readyState.get("state"))

        return instance

    @staticmethod
    def load_resource_changes(changes):
        resourceChanges = ResourceChanges()
        if changes:
            for k, change in changes.items():
                status = change.pop(".status", None)
                if isinstance(status, dict):
                    status = Manifest.load_status(status).local_status
                else:
                    status = to_enum(Status, status)
                resourceChanges[k] = [
                    status,
                    change.pop(".added", None),
                    change,
                ]
        return resourceChanges

    def load_config_change(self, changeSet):
        """
        Reconstruct the Configuration that was applied in the past
        """
        from .configurator import Dependency

        configChange = ConfigChange()
        Manifest.load_status(changeSet, configChange)
        configChange.changeId = changeSet.get("changeId", 0)
        configChange.previousId = changeSet.get("previousId")
        configChange.target = changeSet.get("target")
        configChange.operation = changeSet.get("implementation", {}).get("operation")

        configChange.inputs = changeSet.get("inputs")
        # 'digestKeys', 'digestValue' but configurator can set more:
        for key in changeSet.keys():
            if key.startswith("digest"):
                setattr(configChange, key, changeSet[key])

        configChange.dependencies = []
        for val in changeSet.get("dependencies", []):
            configChange.dependencies.append(
                Dependency(
                    val["ref"],
                    val.get("expected"),
                    val.get("schema"),
                    val.get("name"),
                    val.get("required"),
                    val.get("wantList", False),
                )
            )

        if "changes" in changeSet:
            configChange.resourceChanges = self.load_resource_changes(
                changeSet["changes"]
            )

        configChange.result = changeSet.get("result")
        configChange.messages = changeSet.get("messages", [])

        # XXX
        # ('action', ''),
        # ('target', ''), # nodeinstance key
        # implementationType: configurator resource | artifact | configurator class
        # implementation: repo:key#commitid | className:version
        return configChange

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

    def _create_requirement(self, key, val):
        # parent will be the capability, should have already been created
        capabilityId = val.get("capability")
        if not capabilityId:
            nodeId = val.get("node")
            if not nodeId:
                raise UnfurlError(f"requirement is missing capability {key}")
        capability = capabilityId and self.get_root_resource().query(capabilityId)
        if not capability or not isinstance(capability, CapabilityInstance):
            raise UnfurlError(f"can not find capability {capabilityId}")
        if capability._relationships is None:
            capability._relationships = []
        return self._create_entity_instance(RelationshipInstance, key, val, capability)

    def create_node_instance(self, rname, resourceSpec, parent=None):
        # if parent property is set it overrides the parent argument
        pname = resourceSpec.get("parent")
        if pname:
            parent = self.get_root_resource().find_resource(pname)
            if parent is None:
                raise UnfurlError(f"can not find parent instance {pname}")

        resource = self._create_entity_instance(
            NodeInstance, rname, resourceSpec, parent
        )
        if resourceSpec.get("capabilities"):
            for key, val in resourceSpec["capabilities"].items():
                self._create_entity_instance(CapabilityInstance, key, val, resource)

        if resourceSpec.get("requirements"):
            for req in resourceSpec["requirements"]:
                key, val = next(iter(req.items()))
                requirement = self._create_requirement(key, val)
                requirement.source = resource
                resource.requirements.append(requirement)

        if resourceSpec.get("artifacts"):
            for key, val in resourceSpec["artifacts"].items():
                self._create_entity_instance(ArtifactInstance, key, val, resource)

        for key, val in resourceSpec.get("instances", {}).items():
            self.create_node_instance(key, val, resource)

        return resource

    def _get_last_change(self, operational):
        if not operational.last_config_change:
            return None
        if not self.changeSets:  # XXX load changesets if None
            return None
        jobId = ConfigChange.get_job_id(operational.last_config_change)
        return self.changeSets.get(jobId)

    def _create_entity_instance(self, ctor, name, status, parent):
        operational = self.load_status(status)
        templateName = status.get("template", name)

        imported = None
        importName = status.get("imported")
        if importName is not None:
            imported = self.imports.find_import(importName)
            if not imported:
                raise UnfurlError(f"missing import {importName}")
            if imported.shadow:
                raise UnfurlError(f"already imported {importName}")
            template = imported.template
        else:
            template = self.load_template(templateName)

        if template is None:
            # not defined in the current model any more, try to retrieve the old version
            if operational.last_config_change:
                changerecord = self._get_last_change(operational)
                template = self.load_template(templateName, changerecord)
        if template is None:
            raise UnfurlError(f"missing resource template {templateName}")
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
            self.imports.set_shadow(importName, instance)
        return instance

    def status_summary(self):
        def summary(instance, indent):
            status = "" if instance.status is None else instance.status.name
            state = instance.state and instance.state.name or ""
            if instance.created:
                if isinstance(instance.created, bool):
                    created = "managed"
                else:
                    created = f"created by {instance.created}"
            else:
                created = ""
            output.append(f"{' ' * indent}{instance} {status} {state} {created}")
            indent += 4
            for child in instance.instances:
                summary(child, indent)

        output = []
        summary(self.rootResource, 0)
        return "\n".join(output)

    def find_path_in_repos(self, path, importLoader=None):  # currently unused
        """
        Check if the file path is inside a folder that is managed by a repository.
        If the revision is pinned and doesn't match the repo, it might be bare
        """
        if self.localEnv:
            return self.localEnv.find_path_in_repos(path, importLoader)
        elif self.repo:
            repo = self.repo
            filePath = repo.findRepoPath(path)
            if filePath is not None:
                return repo, filePath, repo.revision, False
        return None, None, None, None

    # NOTE: all the methods below may be called during config parse time via loadYamlInclude()
    def find_repo_from_git_url(self, path, isFile, importLoader):
        repoURL, filePath, revision = split_git_url(path)
        if not repoURL:
            raise UnfurlError(f"invalid git URL {path}")
        assert self.localEnv
        basePath = get_base_dir(importLoader.path)  # checks if dir or not
        repo, revision, bare = self.localEnv.find_or_create_working_dir(
            repoURL, revision, basePath
        )
        return repo, filePath, revision, bare

    def add_repository(self, repo, toscaRepository, file_name):
        repository = self.repositories.get(toscaRepository.name)
        if repository:
            # already exist, make sure it's the same repo
            repo = repo or repository.repo
            if (
                repository.repo and repository.repo != repo
            ) or repository.repository.tpl != toscaRepository.tpl:
                raise UnfurlError(
                    f'Repository "{toscaRepository.name}" already defined'
                )
        self.repositories[toscaRepository.name] = RepoView(
            toscaRepository, repo, file_name
        )
        return repository

    def update_repositories(self, config, inlineRepositories=None, resolver=None):
        if not resolver:
            resolver = self.get_import_resolver(self)
        repositories = self._get_repositories(config)
        for name, tpl in repositories.items():
            if name not in self.repositories:
                toscaRepository = resolver.get_repository(name, tpl)
                self.repositories[name] = RepoView(toscaRepository, None)
        if inlineRepositories:
            for name, repository in inlineRepositories.items():
                if name in self.repositories:
                    logger.verbose(
                        'skipping inline repository definition for "%s", it was previously defined',
                        name,
                    )
                    continue
                if isinstance(repository, dict):
                    repository = Repository(name, repository)
                self.add_repository(None, repository, "")
        return self._set_repositories()

    @staticmethod
    def _get_repositories(tpl):
        return ((tpl.get("spec") or {}).get("service_template") or {}).get(
            "repositories"
        ) or {}

    def _set_repositories(self):
        repositories = self.repositories
        if "unfurl" not in repositories:
            # add a repository that points to this package
            repository = RepoView(
                Repository("unfurl", dict(url="file:" + _basepath)),
                None,
                "",
            )
            repositories["unfurl"] = repository
        if "self" not in repositories:
            # this is called too early to use self.getBaseDir()
            path = get_base_dir(self.path) if self.path else "."
            if self.repo:
                url = self.repo.get_url_with_path(path)
                path = os.path.relpath(path, self.repo.working_dir)
            else:
                url = "file:" + path
            repository = RepoView(
                Repository("self", dict(url=url)),
                self.repo,
                path,
            )
            repositories["self"] = repository

        inProject = False
        if self.localEnv and self.localEnv.project:
            if self.localEnv.project is self.localEnv.homeProject:
                inProject = self.localEnv.project.projectRoot in self.path
            else:
                inProject = True
        if inProject and "project" not in repositories:
            repositories["project"] = self.localEnv.project.project_repoview

        if "spec" not in repositories:
            # if not found assume it points the project root or self if not in a project
            if inProject:
                repositories["spec"] = self.localEnv.project.project_repoview
            else:
                repositories["spec"] = repositories["self"]
        return {name: repo.repository.tpl for name, repo in self.repositories.items()}

    def load_yaml_include(
        self,
        yamlConfig,
        templatePath,
        baseDir,
        warnWhenNotFound=False,
        expanded=None,
        check=False,
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

                inlineRepository = {reponame: repo}
        else:
            artifactTpl = dict(file=templatePath)

        if check:
            if not inlineRepository and "repository" in artifactTpl:
                reponame = artifactTpl["repository"]
                if reponame in ["spec", "self", "unfurl"]:  # builtin
                    return True
                repositories = self._get_repositories(expanded)
                return reponame in repositories
            return True

        artifact = ArtifactSpec(artifactTpl, path=baseDir)

        tpl = CommentedMap()

        resolver = self.get_import_resolver(warnWhenNotFound)
        tpl["repositories"] = self.update_repositories(
            expanded or yamlConfig.config, inlineRepository, resolver
        )
        loader = toscaparser.imports.ImportsLoader(
            None,
            artifact.base_dir,
            tpl=tpl,
            resolver=resolver,
        )
        return loader._load_import_template(None, artifact.as_import_spec())

    def get_import_resolver(self, ignoreFileNotFound=False, expand=False):
        return ImportResolver(self, ignoreFileNotFound, expand)


class SnapShotManifest(Manifest):
    def __init__(self, manifest, commitId):
        super().__init__(manifest.path, self.localEnv)
        self.commitId = commitId
        oldManifest = manifest.repo.show(manifest.path, commitId)
        self.repo = manifest.repo
        self.localEnv = manifest.localEnv
        self.manifest = YamlConfig(
            oldManifest, manifest.path, loadHook=self.load_yaml_include
        )
        self.update_repositories(oldManifest)
        expanded = self.manifest.expanded
        # just needs the spec, not root resource
        self._set_spec(expanded)
        self._ready(None)
