# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
from collections.abc import Mapping
import datetime
import os.path
import hashlib
import json
from pathlib import Path
from typing import Dict, List, Optional, Any, Sequence, TYPE_CHECKING, Tuple, cast
from urllib.parse import urlparse
from ruamel.yaml.comments import CommentedMap

from .lock import Lock

from .spec import EntitySpec, NodeSpec, ToscaSpec, TOSCA_VERSION, ArtifactSpec

from .support import (
    ResourceChanges,
    AttributeManager,
    Status,
    Priority,
    NodeState,
    Imports,
)
from .runtime import (
    EntityInstance,
    HasInstancesInstance,
    OperationalInstance,
    NodeInstance,
    CapabilityInstance,
    RelationshipInstance,
    ArtifactInstance,
    TopologyInstance,
)
from .util import (
    UnfurlError,
    assert_not_none,
    is_relative_to,
    to_enum,
    sensitive_str,
    get_base_dir,
    taketwo,
)
from .repo import normalize_git_url, split_git_url, RepoView, GitRepo
from .packages import (
    Package,
    PackageSpec,
    PackagesType,
    find_canonical,
    get_package_id_from_url,
)
from .merge import merge_dicts
from .result import ChangeRecord, ResourceRef
from .yamlloader import yaml, ImportResolver, yaml_dict_type, SimpleCacheResolver
from .logs import getLogger
from . import DEFAULT_CLOUD_SERVER, __version__
import toscaparser.imports
from toscaparser.repositories import Repository
import tosca
from tosca.loader import install

if TYPE_CHECKING:
    from .localenv import LocalEnv
    from ansible.parsing.yaml.loader import AnsibleLoader

logger = getLogger("unfurl")

_basepath = os.path.abspath(os.path.dirname(__file__))


def relabel_dict(environment: Dict, localEnv: "LocalEnv", key: str) -> Dict[str, Any]:
    """Retrieve environment dictionary and remap any values that are strings in the dictionary by treating them as keys into an environment."""
    connections = environment.get(key)
    if not connections:
        return {}
    assert isinstance(connections, dict)
    environments: Dict[str, Dict] = {}
    if localEnv:
        project = localEnv.project or localEnv.homeProject
        if project:
            environments = project.contexts  # type: ignore

    # handle items like newname : oldname to rename merged connections
    def follow_alias(v):
        if isinstance(v, str):
            env, sep, name = v.partition(":")
            if sep:  # found a ":"
                v = environments[env][key][name]
            else:  # look in current dict
                v = connections[env]  # type: ignore
            return follow_alias(v)  # follow
        else:
            return v

    return dict((n, follow_alias(v)) for n, v in connections.items())


class ChangeRecordRecord(ChangeRecord, OperationalInstance):
    target: str = ""
    operation: str = ""


class Manifest(AttributeManager):
    """
    Base class for managing an ensemble.
    Derived classes handle loading and serialization.
    """

    rootResource: Optional[TopologyInstance] = None

    def __init__(self, path: Optional[str], localEnv: Optional["LocalEnv"] = None):
        super().__init__(yaml)
        self.localEnv = localEnv
        self.path: Optional[str] = path
        self.repo = self._find_repo()
        self.currentCommitId = self.repo and self.repo.revision
        # self.revisions = RevisionManager(self)
        self.changeSets: Optional[Dict[str, ChangeRecordRecord]] = None
        self.tosca: Optional[ToscaSpec] = None
        self.specDigest = None
        self.repositories: Dict[str, RepoView] = {}
        self.package_specs: List[PackageSpec] = []
        self.packages: PackagesType = {}
        if self.localEnv:
            # before we start parsing the manifest, add the repositories in the environment
            self._add_repositories_from_environment()
            self.cache = self.localEnv.loader_cache
        else:
            self.cache = {}
        self.imports = Imports()
        self.imports.manifest = self
        self._importedManifests: Dict = {}
        self.modules: Optional[Dict] = None

    def _add_repositories_from_environment(self) -> None:
        assert self.localEnv
        context = self.localEnv.get_context()
        repositories = {}
        for key, value in relabel_dict(context, self.localEnv, "repositories").items():
            if "." in key:  # assume it's a package not a repository name
                assert isinstance(value, dict)
                self.package_specs.append(
                    PackageSpec(key, value.get("url"), value.get("revision"))
                )
            else:
                repositories[key] = value
        env_package_spec = context.get("variables", {}).get(
            "UNFURL_PACKAGE_RULES", os.getenv("UNFURL_PACKAGE_RULES")
        )
        if env_package_spec:
            for key, value in taketwo(env_package_spec.split()):
                self.package_specs.append(PackageSpec(key, value, None))
        resolver = self.get_import_resolver()
        for name, tpl in repositories.items():
            toscaRepository = resolver.get_repository(name, tpl)
            assert toscaRepository
            self.repositories[name] = RepoView(toscaRepository, None)

    def _set_spec(self, spec, more_spec=None, skip_validation=False, fragment=""):
        """
        Set the TOSCA service template.
        """
        repositories = {
            name: repo.repository.tpl for name, repo in self.repositories.items()
        }
        self.tosca = self._load_spec(
            spec, self.path, repositories, more_spec, skip_validation, fragment
        )
        self.specDigest = self.get_spec_digest(spec)
        tosca.global_state.mode = "runtime"

    def _find_repo(self) -> Optional["GitRepo"]:
        # check if this path exists in the repo
        repo = self.localEnv and self.localEnv.instance_repoview
        if repo:
            # this check is expensive and not that important so skip
            return repo.repo
            # path = repo.find_path(self.path)[0]
            # if path and (path, 0) in repo.repo.index.entries:
            #     return repo
        return None

    def _load_spec(
        self, spec, path, repositories, more_spec, skip_validation, fragment
    ) -> ToscaSpec:
        if "service_template" in spec:
            toscaDef = spec["service_template"] or {}
            fragment += "/service_template"
        elif "tosca" in spec:  # backward compat
            toscaDef = spec["tosca"] or {}
            fragment += "/tosca"
        else:
            toscaDef = {}
            fragment = ""

        repositoriesTpl = toscaDef.setdefault("repositories", CommentedMap())
        for name, value in repositories.items():
            if name not in repositoriesTpl:
                repositoriesTpl[name] = value

        # make sure this is present
        toscaDef["tosca_definitions_version"] = TOSCA_VERSION
        if more_spec:
            # don't merge individual templates
            toscaDef = merge_dicts(
                toscaDef,
                more_spec,
                replaceKeys=["node_templates", "relationship_templates"],
            )
        yaml_dict_cls = yaml_dict_type(bool(self.localEnv and self.localEnv.readonly))
        if not isinstance(toscaDef, yaml_dict_cls):
            toscaDef = yaml_dict_cls(toscaDef.items())
        if getattr(toscaDef, "base_dir", None) and (
            not path or toscaDef.base_dir != os.path.dirname(path)
        ):
            # note: we only recorded the baseDir not the name of the included file
            path = toscaDef.base_dir
        return ToscaSpec(
            toscaDef,
            spec,
            path,
            self.get_import_resolver(expand=True),
            skip_validation,
            fragment,
        )

    def get_spec_digest(self, spec):
        m = hashlib.sha1()  # use same digest function as git
        assert self.tosca
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
            rootResource.set_attribute_manager(self)

    def get_root_resource(self):
        return self.rootResource

    def get_base_dir(self):
        return "."

    @property
    def deployment(self) -> str:
        return os.path.basename(os.path.dirname(self.path)) if self.path else ""

    @property
    def loader(self) -> Optional["AnsibleLoader"]:
        return (
            self.rootResource
            and self.rootResource.templar
            and self.rootResource.templar._loader
        )

    def save_job(self, job):
        pass

    def load_error(self, msg: str) -> None:
        if self.validate:
            raise UnfurlError(msg)
        else:
            logger.error(msg)
        return None

    def load_template(
        self, name: str, parent: Optional[EntityInstance], lastChange=None
    ) -> Optional[EntitySpec]:
        if lastChange:
            return None
            # XXX implement revisions
            # try:
            #     return self.revisions.get_revision(lastChange).tosca.get_template(name)
            # except Exception:
            #     return None
        elif parent:
            return parent.template.get_template(name)
        elif self.tosca:
            return self.tosca.get_template(name)
        else:
            return None

    #  load instances
    #    create a resource with the given template
    #  or generate a template setting interface with the referenced implementations

    @staticmethod
    def load_status(status, instance: Optional[OperationalInstance] = None):
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
            instance._lastStatus = to_enum(Status, readyState.get("effective"))
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

    def load_config_change(self, changeSet: dict) -> ChangeRecordRecord:
        """
        Reconstruct the Configuration that was applied in the past
        """
        from .configurator import Dependency

        configChange = ChangeRecordRecord()
        Manifest.load_status(changeSet, configChange)
        configChange.changeId = changeSet.get("changeId", 0)
        configChange.previousId = changeSet.get("previousId")
        configChange.target = changeSet.get("target", "")
        assert isinstance(configChange.target, str)
        configChange.operation = changeSet.get("implementation", {}).get(
            "operation", ""
        )
        assert isinstance(configChange.operation, str)

        configChange.inputs = changeSet.get("inputs")  # type: ignore
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
            configChange.resourceChanges = self.load_resource_changes(  # type: ignore
                changeSet["changes"]
            )

        configChange.result = changeSet.get("result")  # type: ignore
        configChange.messages = changeSet.get("messages", [])  # type: ignore

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

    def _create_requirement(self, key, val, root) -> Optional[RelationshipInstance]:
        # parent will be the capability, should have already been created
        capabilityId = val.get("capability")
        if not capabilityId:
            nodeId = val.get("node")
            if not nodeId:
                logger.warning(
                    f"skipping requirement {key}: no node or capability specified"
                )
                return None
        capability = capabilityId and root.query(capabilityId)
        if not capability or not isinstance(capability, CapabilityInstance):
            return self.load_error(f"can not find capability {capabilityId}")  # type: ignore
        if capability._relationships is None:
            capability._relationships = []
        return self._create_entity_instance(RelationshipInstance, key, val, capability)

    def _create_substituted_topology(
        self, rname: str, resourceSpec: dict, parent: Optional[EntityInstance]
    ) -> Optional[TopologyInstance]:
        root = cast(
            TopologyInstance, parent.root if parent else self.get_root_resource()
        )
        templateName = resourceSpec.get("template", rname)
        template = cast(Optional[NodeSpec], self.load_template(templateName, parent))
        if template is None:
            self.load_error(
                f"missing template definition for '{templateName}' while instantiating instance '{rname}'"
            )
            return None
        substitution = template.substitution
        if substitution is None:
            self.load_error(
                f"missing substitution in template while instantiating instance '{rname}'"
            )
            return None
        status = resourceSpec["substitution"]
        operational = self.load_status(status)
        topology_instance = root.create_nested_topology(substitution, operational)
        assert root.imports is not None
        inner_name = (
            substitution.substitution_node and substitution.substitution_node.name
        )
        for key, val in status.get("instances", {}).items():
            self.create_node_instance(key, val, topology_instance)
        if inner_name:
            inner = topology_instance.find_instance(inner_name)
            if inner:
                root.imports.add_import(":" + inner.template.nested_name, inner)
        return topology_instance

    def create_node_instance(
        self,
        rname: str,
        resourceSpec: Dict[str, Any],
        parent: HasInstancesInstance,
    ) -> NodeInstance:
        # if parent property is set it overrides the parent argument
        root: ResourceRef = assert_not_none(parent.root)
        pname = resourceSpec.get("parent")
        if pname:
            _parent = root.find_instance(pname)  # type: ignore
            if _parent is None:
                self.load_error(f"can not find parent instance {pname} for {rname}")
            parent = _parent

        if resourceSpec.get("substitution"):
            # need to create the nested topology before a NodeInstance that has "imported"
            self._create_substituted_topology(rname, resourceSpec, parent)

        resource = self._create_entity_instance(
            NodeInstance, rname, resourceSpec, parent
        )
        if resourceSpec.get("capabilities"):
            for key, val in resourceSpec["capabilities"].items():
                self._create_entity_instance(CapabilityInstance, key, val, resource)

        if resourceSpec.get("requirements"):
            for req in resourceSpec["requirements"]:
                key, val = next(iter(req.items()))
                requirement = self._create_requirement(key, val, root)
                if not requirement:
                    continue
                requirement._source = resource
                assert (
                    requirement in resource.requirements
                ), f"{requirement} not in {resource.requirements} for {rname}"

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
        jobId = ChangeRecord.get_job_id(operational.last_config_change)
        return self.changeSets.get(jobId)

    def _create_entity_instance(
        self, ctor, name: str, status: Dict[str, Any], parent: EntityInstance
    ):
        templateName = status.get("template", name)

        imported = None
        importName = status.get("imported")
        if importName is not None:
            imported = self.imports.find_import(importName)
            if not imported:
                self.load_error(f"missing import {importName}")

        if imported:
            imported_status = dict(
                readyState=dict(
                    local=imported.local_status,
                    effective=imported._lastStatus, 
                )
            )
            operational = self.load_status(imported_status)
        else:
            operational = self.load_status(status)
        if isinstance(templateName, str):
            template = self.load_template(templateName, parent)
        else:
            # special case inline artifact template
            assert isinstance(templateName, dict)
            assert ctor is ArtifactInstance
            template = ArtifactSpec(templateName, parent.template)

        if template is None:
            # not defined in the current model any more, try to retrieve the old version
            if operational.last_config_change:
                changerecord = self._get_last_change(operational)
                template = self.load_template(templateName, parent, changerecord)
        if template is None:
            return self.load_error(
                f"missing template definition for '{templateName}' while instantiating instance '{name}'"
            )
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
        if "protected" in status:
            instance.protected = status["protected"]
        if imported:
            instance.imported = importName
            self.imports.set_shadow(importName, instance, imported)
        properties = status.get("properties")
        if isinstance(properties, dict):
            instance._properties = properties
        return instance

    @staticmethod
    def is_instantiated(resource, checkstatus=True) -> bool:
        if "virtual" in resource.template.directives:
            return False
        if not resource.last_change and (
            not resource.local_status
            or (
                checkstatus
                and resource.local_status in [Status.unknown, Status.ok, Status.pending]
            )
        ):
            return False
        return True

    def status_summary(self, verbose=False):
        def summary(instance, indent, show_virtual=True):
            instantiated = self.is_instantiated(instance)
            computed = " computed " if verbose and instance.is_computed() else ""
            status = "" if instance.status is None else instance.status.name
            state = instance.state and instance.state.name or ""
            if instance.created:
                if isinstance(instance.created, bool):
                    created = "managed"
                else:
                    created = f"created by {instance.created}"
            else:
                created = ""
            instance_label = f"{instance.__class__.__name__}('{instance.nested_name}')"
            if verbose:
                instance_label += f"({instance.template.global_type})"
            local = (
                f"({'None' if instance.local_status is None else instance.local_status.name})"
                if verbose
                else ""
            )
            if instantiated or verbose:
                vlabel = "" if instantiated else " virtual"
                output.append(
                    f"{' ' * indent}{instance_label}{vlabel}{computed} {status}{local} {state} {created}"
                )
                indent += 4
            elif show_virtual:
                output.append(f"{' ' * indent}{instance_label} virtual{computed}")
                indent += 4
            if isinstance(instance, HasInstancesInstance):
                for rel in instance.requirements:
                    summary(rel, indent, False)
                if getattr(instance.template, "substitution", None) and instance.shadow:
                    summary(instance.shadow.root, indent)
                for child in instance.instances:
                    summary(child, indent)
                if verbose:
                    for child in instance.artifacts.values():
                        summary(child, indent)

        output: List[str] = []
        summary(self.rootResource, 0)
        return "\n".join(output)

    def last_commit_time(self) -> Optional[datetime.datetime]:
        # return seconds (0 if not found)
        repo = self.repo
        if not repo:
            return None
        try:
            # find the revision that last modified this file before or equal to the current revision
            # (use current revision to handle branches)
            commits = list(
                repo.repo.iter_commits(repo.revision, self.path or "", max_count=1)
            )
        except ValueError:
            return None
        if commits:
            return commits[0].committed_datetime
        return None

    def get_package_url(self) -> str:
        if self.repo:
            url = self.repo.url
        else:
            url = (
                self.tosca.topology.path or ""
                if self.tosca and self.tosca.topology
                else ""
            )
        if url and self.package_specs:
            namespace_id, _, _ = get_package_id_from_url(url)
            canonical = urlparse(DEFAULT_CLOUD_SERVER).hostname
            if namespace_id and canonical:
                return find_canonical(self.package_specs, canonical, namespace_id)
        return url or ""

    def find_path_in_repos(self, path, importLoader=None):
        """
        Check if the file path is inside a folder that is managed by a repository.
        If the revision is pinned and doesn't match the repo, it might be bare
        """
        if self.localEnv:
            return self.localEnv.find_path_in_repos(path, importLoader)
        elif self.repo:
            repo = self.repo
            filePath = repo.find_repo_path(path)
            if filePath is not None:
                return repo, filePath, repo.revision, False
        return None, None, None, None

    # NOTE: all the methods below may be called during config parse time via loadYamlInclude()

    def find_repo_from_git_url(self, path, base):
        revision: Optional[str]
        repoURL, filePath, revision = split_git_url(path)
        if not repoURL:
            raise UnfurlError(f"invalid git URL {path}")
        assert self.localEnv
        repo, revision, bare = self.localEnv.find_or_create_working_dir(
            repoURL, revision, base
        )
        return repo, filePath, revision, bare

    def add_repository(self, toscaRepository: Repository, file_name: str) -> RepoView:
        # add or replace the repository
        repository: Optional[RepoView] = self.repositories.get(toscaRepository.name)
        if repository:
            # already exist, make sure it's the same repo
            if repository.repository.tpl != toscaRepository.tpl:
                raise UnfurlError(
                    f'Repository "{toscaRepository.name}" already defined'
                )
        repository = RepoView(toscaRepository, None, file_name)
        self.repositories[toscaRepository.name] = repository
        return repository

    def _update_repositories(
        self, config, inlineRepositories=None, resolver: Optional[ImportResolver] = None
    ) -> None:
        # Called during parse time when including files
        if not resolver:
            resolver = self.get_import_resolver()
        # we need to fetch this every call since the config might have changed:
        repositories = self._get_repositories(config)
        lock = config.get("lock")
        if lock and "package_rules" in lock:
            package_specs = [
                PackageSpec(*spec.split()) for spec in lock.get("package_rules", [])
            ]
            if package_specs and not self.package_specs:
                # only use lock section package rules if the environment didn't set some already
                logger.debug(
                    "applying package rules from lock section: %s", package_specs
                )
                self.package_specs = package_specs

        for name, tpl in repositories.items():
            # only set if we haven't seen this repository before
            toscaRepository = resolver.get_repository(name, tpl)
            if toscaRepository:
                try:
                    self.add_repository(toscaRepository, "")
                except UnfurlError:
                    # just warn if the repository is redefined
                    logger.warning(
                        f"Ignoring redefinition of repository {name} to {tpl}"
                    )

        if inlineRepositories:
            for name, repository in inlineRepositories.items():
                if name in self.repositories:
                    logger.verbose(
                        'skipping inline repository definition for "%s", it was previously defined',
                        name,
                    )
                    continue
                if isinstance(repository, dict):
                    repository = resolver.get_repository(name, repository)
                inline_repoview = self.add_repository(repository, "")
                inline_repoview.repository.tpl.setdefault("metadata", {})[
                    "inline"
                ] = True
        self._set_builtin_repositories()

    def _set_repository_links(self):
        if self.localEnv and not self.localEnv.readonly:
            repos = {
                normalize_git_url(r.url): r for r in self.localEnv._get_git_repos()
            }
            for name, repo_view in self.repositories.items():
                if not repo_view.repo and repo_view.url in repos:
                    repo_view.repo = repos[repo_view.url]
                if repo_view.repo and name not in ["self", "project"]:
                    if repo_view.repository.tpl.get("metadata", {}).get("inline"):
                        continue
                    # create tosca_repositories symlink if needed
                    project_base_path = (
                        self.localEnv.project.projectRoot
                        if self.localEnv.project
                        else self.get_base_dir()
                    )
                    repo_view.get_link(project_base_path, name)

    @staticmethod
    def _get_repositories(tpl) -> Dict:
        repositories = ((tpl.get("spec") or {}).get("service_template") or {}).get(
            "repositories"
        ) or {}
        # these take priority:
        repositories.update((tpl.get("environment") or {}).get("repositories") or {})
        return repositories

    def _set_builtin_repositories(self):
        repositories = self.repositories
        if "github.com/onecommons/unfurl" not in self.packages:
            # add a package rule so the unfurl package uses the local installed location
            # (note that it can't set the revision because don't want to overwrite the request version
            unfurl_package_spec = PackageSpec(
                "github.com/onecommons/unfurl", "file:" + _basepath, None
            )
            self.package_specs.append(unfurl_package_spec)
            # add a package with the installed version so that package resolution will detect version incompatibility
            unfurl_package = Package(
                "github.com/onecommons/unfurl", "file:" + _basepath, __version__()
            )
            repository = RepoView(
                Repository("unfurl", dict(url=unfurl_package.url)),
                None,
            )
            unfurl_package.add_reference(repository)
            self.packages["github.com/onecommons/unfurl"] = unfurl_package
            if "unfurl" not in repositories:
                # the user didn't declare one
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
            repository.package = False
            repositories["self"] = repository

        inProject = False
        if self.localEnv and self.localEnv.project:
            if self.localEnv.project is self.localEnv.homeProject:
                inProject = self.localEnv.project.projectRoot in self.path  # type: ignore
            else:
                inProject = True
        if inProject and "project" not in repositories:
            repositories["project"] = self.localEnv.project.project_repoview  # type: ignore
            repositories["project"].package = False

        if "spec" not in repositories:
            # if not found assume it points the project root or self if not in a project
            if inProject:
                assert self.localEnv and self.localEnv.project
                repositories["spec"] = self.localEnv.project.project_repoview  # type: ignore
            else:
                repositories["spec"] = repositories["self"]
            repositories["spec"].package = False

    def repositories_as_tpl(self) -> Dict[str, Dict[str, Any]]:
        return {name: repo.repository.tpl for name, repo in self.repositories.items()}

    def load_yaml_include(
        self,
        yamlConfig,
        templatePath,
        baseDir,
        warnWhenNotFound=False,
        expanded=None,
        action=None,
        repository_root=None,
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
                reponame = repo.get("name", os.path.basename(path))
                # replace spec with just its name
                artifactTpl["repository"] = reponame
                assert repo.get("url"), f"bad inline repository definition: {repo}"
                inlineRepository = {reponame: dict(url=repo["url"])}
        else:
            artifactTpl = dict(file=templatePath)

        if action:
            if action.get_key:
                return action.get_key
            if action.check_include:
                if not inlineRepository and "repository" in artifactTpl:
                    reponame = artifactTpl["repository"]
                    if reponame in ["spec", "self", "unfurl"]:  # builtin
                        return True
                    repositories = self._get_repositories(expanded)
                    return reponame in repositories
                return True
            return None  # unsupported action

        if not artifactTpl["file"]:
            msg = f"document include {templatePath} missing file (base: {baseDir})"
            if warnWhenNotFound:
                logger.warning(msg)
                return "", None
            raise UnfurlError(msg)

        resolver = self.get_import_resolver(warnWhenNotFound, config=expanded)
        self._update_repositories(
            expanded or yamlConfig.config, inlineRepository, resolver
        )
        repositories = self.repositories_as_tpl()
        base_dir = get_base_dir(baseDir)
        if repository_root is None:
            if self.localEnv and self.localEnv.project:
                repository_root = self.localEnv.project.projectRoot
            else:
                repository_root = self.get_base_dir()
            if not is_relative_to(baseDir, repository_root):
                # when including relative files inside a repository we don't know the repository we are in
                # but we might already be out of the project root
                # in that case, repository_root to the base_dir
                # this prevents visiting a parent directory but it is safe if we have gotten this far already
                repository_root = base_dir
        try:
            install(resolver, repository_root)
            loader = toscaparser.imports.ImportsLoader(
                None,
                base_dir,
                repositories=repositories,
                resolver=resolver,
                repository_root=repository_root,
            )
            import_spec = dict(
                file=artifactTpl["file"], repository=artifactTpl.get("repository")
            )
            base, path, doc = loader.load_yaml(import_spec)
            if doc is None:
                logger.warning(
                    f"document include {templatePath} does not exist (base: {baseDir})"
                )
        finally:
            install(None)
        return path, doc

    def get_import_resolver(
        self,
        ignoreFileNotFound: bool = False,
        expand: bool = False,
        config: Optional[dict] = None,
    ) -> ImportResolver:
        if self.localEnv and self.localEnv.make_resolver:
            return self.localEnv.make_resolver(self, ignoreFileNotFound, expand, config)
        return SimpleCacheResolver(self, ignoreFileNotFound, expand, config)


# unused
# class SnapShotManifest(Manifest):
#     def __init__(self, manifest, commitId):
#         super().__init__(manifest.path, self.localEnv)
#         self.commitId = commitId
#         oldManifest = manifest.repo.show(manifest.path, commitId)
#         self.repo = manifest.repo
#         self.localEnv = manifest.localEnv
#         self.manifest = YamlConfig(
#             oldManifest, manifest.path, loadHook=self.load_yaml_include
#         )
#         self._update_repositories(oldManifest)
#         expanded = self.manifest.expanded
#         # just needs the spec, not root resource
#         self._set_spec(expanded.get("spec", {}))
#         self._ready(None)
