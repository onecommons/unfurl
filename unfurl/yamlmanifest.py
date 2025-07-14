# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
"""Loads and saves a ensemble manifest."""

import io
import json
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Optional,
    Tuple,
    TYPE_CHECKING,
    TypedDict,
    cast,
)
from typing_extensions import NotRequired
import sys
from collections.abc import MutableSequence, Mapping
import numbers
import os
import os.path
from pathlib import Path
import itertools

try:
    # added in python 3.9
    from functools import cache  # type: ignore
except ImportError:
    from functools import lru_cache as cache

from . import DefaultNames
from .util import (
    UnfurlError,
    UnfurlValidationError,
    get_base_dir,
    substitute_env,
    to_yaml_text,
    filter_env,
)
from .merge import patch_dict
from .yamlloader import YamlConfig, load_yaml, make_yaml, cleartext_yaml
from .result import serialize_value
from .support import ResourceChanges, Defaults, Status
from .localenv import LocalEnv
from .lock import Lock
from .manifest import Manifest, relabel_dict, ChangeRecordRecord
from .packages import is_semver_compatible_with, is_semver
from .spec import (
    ArtifactSpec,
    NodeSpec,
    RelationshipSpec,
    ToscaSpec,
    encode_unfurl_identifier,
    find_env_vars,
    get_default_topology,
)
from .runtime import (
    EntityInstance,
    NodeInstance,
    TopologyInstance,
    RelationshipInstance,
)
from .eval import map_value, Ref
from .planrequests import create_instance_from_spec
from .logs import getLogger
from .init import get_input_vars
from ruamel.yaml.comments import CommentedMap
from codecs import open
from ansible.parsing.dataloader import DataLoader

if TYPE_CHECKING:
    from .job import Job, ConfigTask

logger = getLogger("unfurl")

_basepath = os.path.abspath(os.path.dirname(__file__))


def save_config_spec(spec):
    saved = CommentedMap([("operation", spec.operation), ("className", spec.className)])
    if spec.majorVersion:
        saved["majorVersion"] = spec.majorVersion
    if spec.minorVersion:
        saved["minorVersion"] = spec.minorVersion
    # if spec.provides:
    #   dotSelf = spec.provides.get('.self')
    #   if dotSelf:
    #     # removed defaults put in by schema
    #     dotSelf.pop('configurations', None)
    #     if not dotSelf.get('attributes'):
    #       dotSelf.pop('attributes', None)
    #   saved["provides"] = spec.provides
    return saved


def save_dependency(dep):
    saved = CommentedMap()
    if dep.name and dep.name != dep.expr:
        saved["name"] = dep.name
    saved["ref"] = dep.expr
    if dep.expected is not None:
        saved["expected"] = serialize_value(dep.expected)
    if dep.schema is not None:
        saved["schema"] = dep.schema
    if dep.required:
        saved["required"] = dep.required
    if dep.wantList:
        saved["wantList"] = dep.wantList
    if dep.write_only:
        saved["writeOnly"] = dep.write_only
    return saved


def save_resource_changes(changes: ResourceChanges):
    d = CommentedMap()
    for k, v in changes.items():
        # k is the resource's key, add its changed attributes
        d[k] = serialize_value(v[ResourceChanges.attributesIndex] or {})
        # add special keys to changes:
        status = cast(Optional[Status], v[ResourceChanges.statusIndex])
        if status is not None:
            d[k][".status"] = status.name
        if v[ResourceChanges.addedIndex]:
            d[k][".added"] = serialize_value(v[ResourceChanges.addedIndex])
    return d


def has_status(operational):
    return operational.last_change or operational.status


def save_status(operational, status: Optional[CommentedMap] = None) -> CommentedMap:
    if status is None:
        status = CommentedMap()
    if not has_status(operational):
        # skip status
        return status

    readyState = CommentedMap()
    if operational.local_status is not None:
        if operational.status != operational.local_status:
            # if different serialize this too
            readyState["effective"] = operational.status.name
        readyState["local"] = operational.local_status.name
    else:
        readyState["effective"] = operational.status.name
    if operational.state is not None:
        readyState["state"] = operational.state.name
    if operational.priority is not None:
        status["priority"] = operational.priority.name
    if not status.get("imported"):
        status["readyState"] = readyState

    if operational.last_state_change:
        status["lastStateChange"] = operational.last_state_change
    if operational.last_config_change:
        status["lastConfigChange"] = operational.last_config_change

    return status


def save_result(value):
    if isinstance(value, Mapping):
        return CommentedMap(
            (key, save_result(v)) for key, v in value.items() if v is not None
        )
    elif isinstance(value, (MutableSequence, tuple)):
        return [save_result(item) for item in value]
    elif value is not None and not isinstance(value, (numbers.Real, bool)):
        return to_yaml_text(value)
    else:
        return value


def save_task(task: "ConfigTask", skip_result=False) -> CommentedMap:
    """
    Convert dictionary suitable for serializing as yaml
      or creating a Changeset.

    .. code-block:: YAML

      changeId:
      target:
      implementation:
      inputs:
      changes:
      dependencies:
      messages:
      outputs:
      result:  # an object or "skipped"
    """
    output = CommentedMap()
    output["changeId"] = task.changeId
    if task.previousId:
        output["previousId"] = task.previousId
    if task.target:
        output["target"] = task.target.key
    save_status(task, output)
    output["implementation"] = save_config_spec(task.configSpec)
    if task.reason:
        output["reason"] = str(task.reason)
    try:
        if task._resolved_inputs:  # only serialize resolved inputs
            output["inputs"] = serialize_value(task._resolved_inputs)
    except Exception:
        logger.error(
            "Error while saving task %s, serializing inputs failed", task, exc_info=True
        )
    changes = save_resource_changes(task._resourceChanges)
    if changes:
        output["changes"] = changes
    if task.messages:
        output["messages"] = task.messages
    dependencies = [save_dependency(val) for val in task.dependencies]
    if dependencies:
        output["dependencies"] = dependencies
    try:
        if task.result:
            if task.result.outputs:
                output["outputs"] = save_result(task.result.outputs)
            if task.result.result and not skip_result:
                output["result"] = save_result(task.result.result)
        else:
            output["result"] = "skipped"
        output.update(task.configurator.save_digest(task))
    except Exception:
        logger.error("Error while saving task %s", task, exc_info=True)
    output["summary"] = task.summary()
    return output


def split_changes(
    changes: List[CommentedMap],
) -> Tuple[List[CommentedMap], List[CommentedMap]]:
    local_changes = []
    committed_changes = []
    for change in changes:
        local_change = change.copy()
        local_changes.append(local_change)
        if "result" in change and change["result"] != "skipped":
            change.pop("result")
        committed_changes.append(change)
    return local_changes, committed_changes


@cache
def get_manifest_schema(format: str) -> dict:
    path = os.path.join(_basepath, "manifest-schema.json")
    with open(path) as fp:
        schema = json.load(fp)
    if format == "blueprint":
        schema["required"].remove("kind")
    return schema


class LfsSettings(TypedDict):
    lock: NotRequired[str]  # require, no, try
    name: NotRequired[str]  # name of the lock $ensemble or $environment
    url: NotRequired[str]  # otherwise use the ensemble's git repository


class ReadOnlyManifest(Manifest):
    """Loads an ensemble from a manifest but doesn't instantiate the instance model."""

    def __init__(
        self,
        manifest=None,
        path: Optional[str] = None,
        validate=True,
        localEnv: Optional[LocalEnv] = None,
        vault=None,
        safe_mode: Optional[bool] = None,
    ):
        path = path or (localEnv.manifestPath if localEnv else None)
        if path:
            path = os.path.abspath(path)
        super().__init__(path, localEnv)
        self._importedManifests: Dict[int, Optional["YamlManifest"]] = {}
        readonly = bool(localEnv and localEnv.readonly)
        self.safe_mode = bool(safe_mode)
        schema = get_manifest_schema(
            localEnv and localEnv.overrides.get("format") or ""
        )
        self.manifest = YamlConfig(
            manifest,
            self.path,
            validate,
            schema,
            self.load_yaml_include,
            vault,
            readonly,
        )
        if self.manifest.path:
            logger.debug("loaded ensemble manifest at %s", self.manifest.path)
        manifest = self.manifest.expanded
        self.apiVersion = manifest.get("apiVersion")
        spec = manifest.get("spec", {})
        self.context = manifest.get("environment", CommentedMap())
        if localEnv:
            self.context = localEnv.get_context(self.context)
        self._update_inputs(spec)
        # _update_repositories might not have been called while parsing
        # call it now to make sure we set up the built-in repositories
        self._update_repositories(manifest)

    def _update_inputs(self, spec: dict) -> None:
        inputs = spec.get("inputs") or {}
        context_inputs = self.context.get("inputs")
        if context_inputs:
            inputs.update(context_inputs)
        if self.localEnv:
            # overrides is set by job from --var options
            s = get_input_vars(self.localEnv.overrides)
            if s:
                job_inputs = load_yaml(
                    self.manifest.yaml, io.StringIO(s), None, self.manifest.readonly
                )
                inputs.update(job_inputs)
        spec["inputs"] = inputs

    @property
    def uris(self) -> List[str]:
        uris: List[str] = []
        if self.manifest.config and "metadata" in self.manifest.config:
            uri = self.metadata.get("uri")
            uris = self.metadata.get("aliases") or []
            if uri:
                return [uri] + uris
        return uris

    @property
    def uri(self) -> str:
        uris = self.uris
        if uris:
            return uris[0]
        else:
            return ""

    def has_uri(self, uri) -> bool:
        return uri in self.uris

    @property
    def metadata(self) -> Dict[str, Any]:
        return self.manifest.config.setdefault("metadata", CommentedMap())

    @property
    def version(self) -> Optional[str]:
        return (
            self.tosca.template.metadata.get("template_version") if self.tosca else None
        )

    @property
    def yaml(self):
        return self.manifest.yaml

    def get_base_dir(self) -> str:
        if self.path:
            return get_base_dir(self.path)
        else:
            return "."

    def is_path_to_self(self, path) -> bool:
        if self.path is None or path is None:
            return False
        return os.path.abspath(self.path) == os.path.abspath(path)

    def get_saved_outputs(self):
        return self.manifest.expanded.get("status", {}).get("outputs")

    # def addRepo(self, name, repo):
    #     self._getRepositories(self.manifest.config)[name] = repo

    def dump(self, out=sys.stdout):
        self.manifest.dump(out)


def clone(localEnv: LocalEnv, destPath) -> ReadOnlyManifest:
    clone = ReadOnlyManifest(localEnv=localEnv)
    config = cast(dict, clone.manifest.config)
    for key in ["status", "changes", "lastJob", "lock"]:
        config.pop(key, None)
    if "metadata" in config:
        config["metadata"].pop("uri", None)
        config["metadata"].pop("aliases", None)
    repositories = Manifest._get_repositories(config)
    repositories.pop("self", None)
    clone.path = destPath
    clone.manifest.path = destPath
    return clone


def _match_deployment_blueprints(deployment_blueprints, context):
    primary_provider_name = context.get("primary_provider", "primary_provider")
    connections = context.get("connections") or {}
    primary_provider = connections.get(primary_provider_name)
    if not primary_provider or "type" not in primary_provider:
        return None
    for name, tpl in deployment_blueprints.items():
        if tpl.get("cloud") == primary_provider["type"]:
            return name
    return None


class YamlManifest(ReadOnlyManifest):
    _operationIndex: Optional[Dict[Tuple[str, str], str]] = None
    lockfilepath = None
    lockfile = None
    lfs_locked: Optional[str] = None
    lfs_url: Optional[str] = None

    def __init__(
        self,
        manifest=None,
        path=None,
        validate=True,  # json schema validation
        localEnv: Optional[LocalEnv] = None,
        vault=None,
        skip_validation=False,  # tosca parser validation
        safe_mode: Optional[bool] = None,
    ):
        super().__init__(manifest, path, validate, localEnv, vault, safe_mode)
        self.validate = not skip_validation  # see AttributeManager.validate
        # instantiate the tosca template
        manifest = self.manifest.expanded
        if self.manifest.path:
            self.lockfilepath = self.manifest.path + ".lock"
        spec = manifest.get("spec", {})
        # load_env_instances is only set when exporting environments
        # otherwise don't include environment instances in the environment
        load_env_instances = self.localEnv and self.localEnv.overrides.get(
            "load_env_instances"
        )
        more_spec = self._load_context(self.context, localEnv, load_env_instances)
        deployment_blueprint = self.context.get("deployment_blueprint")
        deployment_blueprints = self.get_deployment_blueprints()
        if deployment_blueprints:
            if (
                not deployment_blueprint
                and localEnv
                and localEnv.manifest_context_name != "defaults"
            ):
                deployment_blueprint = _match_deployment_blueprints(
                    deployment_blueprints, self.context
                )
            if deployment_blueprint:
                if self._add_deployment_blueprint_template(
                    deployment_blueprints, deployment_blueprint, more_spec
                ):
                    logger.info('Using deployment blueprint "%s"', deployment_blueprint)
            else:
                logger.warning(
                    "This ensemble contains deployment blueprints but none were specified for use."
                )

        # need to load external ensembles before we cal _set_spec
        importsSpec = self.context.get("external", {})
        # note: external "localhost" is defined in UNFURL_HOME's context by convention
        connections = []
        for name, value in importsSpec.items():
            connections.extend(self.load_external_ensemble(name, value))
        self.imports.connections = connections

        if self.context.get("instances") and load_env_instances:
            # add context instances to spec instances but skip ones that are just in there because they were shared
            env_instances = {
                k: v.copy()
                for k, v in self.context["instances"].items()
                if "imported" not in v
            }
            self._load_resource_templates(
                env_instances, spec.setdefault("instances", {}), True
            )
        self._set_spec(spec, more_spec, skip_validation, "spec")
        assert self.tosca
        if self.localEnv:
            msg = f'Loading ensemble "{self.path}" in environment "{self.localEnv.manifest_context_name}"'
            if self.tosca.topology and self.tosca.topology.primary_provider:
                msg += f' with a primary_provider of type "{self.tosca.topology.primary_provider.type}"'
            logger.info(msg)

        status = manifest.get("status", {})
        self.changeLogPath: str = manifest.get("jobsLog") or ""
        if not self.changeLogPath and localEnv and manifest.get("changes") is None:
            # save changes to a separate file if we're in a local environment
            self.changeLogPath = DefaultNames.JobsLog
        self.load_changes(manifest.get("changes"), self.changeLogPath)

        self.lastJob: Optional[dict] = manifest.get("lastJob")

        if localEnv:
            for name in ["locals", "secrets"]:
                instance, local_spec = localEnv.get_local_instance(name, self.context)
                self.imports.add_import(name.rstrip("s"), instance, local_spec)

        rootResource = self.create_topology_instance(status)
        for key, val in status.get("instances", {}).items():
            self.create_node_instance(key, val, rootResource)
        # create an new instances declared in the spec:
        for name, instance_tpl in spec.get("instances", {}).items():
            if not rootResource.find_resource(name):
                if "readyState" not in instance_tpl:
                    instance_tpl["readyState"] = "ok"
                create_instance_from_spec(self, rootResource, name, instance_tpl)

        if self._load_errors and not skip_validation:
            raise UnfurlValidationError(
                "Error loading ensemble, see logs for errors",
            )
        self._configure_root(rootResource)
        self._set_repository_links()
        self._ready(rootResource)

    def get_deployment_blueprints(self) -> Dict[str, dict]:
        spec = self.manifest.expanded.get("spec", {})
        deployment_blueprints = spec.get("deployment_blueprints") or {}
        if spec.get("service_template", {}).get("deployment_blueprints"):
            st_dps = spec["service_template"].get("deployment_blueprints")
            if st_dps:
                # outer deployment_blueprints override the service_template one (UI sets the outer one)
                st_dps.update(deployment_blueprints)
                return st_dps
        return deployment_blueprints

    def _add_deployment_blueprint_template(
        self, deployment_blueprints, deployment_blueprint, more_spec
    ):
        if deployment_blueprint not in deployment_blueprints:
            msg = f"Can not find requested deployment blueprint: '{deployment_blueprint}' is missing from the ensemble."
            if self.validate:
                raise UnfurlError(msg)
            else:
                logger.error(msg)
            return False
        deployment_blueprint_tpl = deployment_blueprints[deployment_blueprint]
        resource_templates = deployment_blueprint_tpl.get(
            "resource_templates"
        ) or deployment_blueprint_tpl.get("node_templates")
        resourceTemplates = deployment_blueprint_tpl.get("resourceTemplates")
        if resourceTemplates is not None:
            # resourceTemplates and ResourceTemplate keys exist when imported from json
            resource_templates = {}
            local_resource_templates = (
                deployment_blueprint_tpl.get("ResourceTemplate") or {}
            )
            for template_name in resourceTemplates:
                if template_name in local_resource_templates:
                    resource_templates[template_name] = local_resource_templates[
                        template_name
                    ]
        if resource_templates:
            node_templates = more_spec["topology_template"]["node_templates"]
            self._load_resource_templates(resource_templates, node_templates, False)
        return True

    def _configure_root(self, rootResource: TopologyInstance) -> None:
        assert rootResource._templar
        if (
            self.manifest.vault and self.manifest.vault.secrets
        ):  # setBaseDir() may create a new templar
            rootResource._templar._loader.set_vault_secrets(self.manifest.vault.secrets)
        if not self.localEnv:
            return

        if self.localEnv.overrides.get("skip_secret_files"):
            return
        # use the password associated with the project the repository appears in.
        repos = set(self.repositories.values())
        project = self.localEnv.project or self.localEnv.homeProject
        while project:
            loader = DataLoader()
            vault = project.make_vault_lib()
            if vault:
                yaml = make_yaml(vault)
                loader.set_vault_secrets(vault.secrets)
                for repoview in project.workingDirs.values():
                    if repoview in repos:
                        repoview.load_secrets(loader)
                        repoview.yaml = yaml
                        repos.remove(repoview)
            project = project.parentProject

        # left over:
        for repository in repos:
            repository.load_secrets(rootResource._templar._loader)
            repository.yaml = self.yaml

    def _set_root_environ(self) -> None:
        # We need to set the environment as early as possible but not too early
        # Each ensemble maintains its own set of environment variables that is used when evaluating expressions (e.g get_env)
        # but os.environ is only set to this when a task is active.
        root = self.rootResource
        assert root
        rules = self.context.get("variables") or CommentedMap()
        _previous_validate = self.validate
        # values maybe wrong before we set up the env vars so disable validation to suppress validation exceptions
        self.validate = False
        try:
            for rel in root.default_relationships:
                rules.update(rel.merge_props(find_env_vars, True))
            rules = cast(
                dict, serialize_value(map_value(rules, root), resolveExternal=True)
            )
            root._environ = filter_env(rules, os.environ)
        finally:
            self.validate = _previous_validate
        paths = self.localEnv and self.localEnv.get_paths()
        if paths:
            path = os.pathsep.join(paths)
            if path not in root._environ["PATH"]:  # avoid setting twice
                root._environ["PATH"] = (
                    path + os.pathsep + root._environ.get("PATH", "")
                )
                logger.debug("PATH set to %s", root._environ["PATH"])

    def create_topology_instance(self, status: dict) -> TopologyInstance:
        """
        If an instance of the topology is recorded in status, load it,
        otherwise create a new resource using the the topology as its template
        """
        # XXX use the substitution_mapping (3.8.12) represent the resource
        operational = self.load_status(status)
        topology = self.tosca and self.tosca.topology
        assert topology
        root = TopologyInstance(topology, operational)
        root.set_attribute_manager(self)
        if os.environ.get("UNFURL_WORKDIR"):
            root.set_base_dir(os.environ["UNFURL_WORKDIR"])
        elif not self.path:
            root.set_base_dir(root.tmp_dir)
        else:
            root.set_base_dir(self.get_base_dir())

        # need to set rootResource before createNodeInstance() is called
        self.rootResource = root
        root.imports = self.imports
        if not self.safe_mode:
            self._set_root_environ()
        return root

    def _load_resource_templates(self, templates, node_templates, virtual):
        # "resource_templates" are node templates that aren't included in the topology_template
        # but are referenced by the deployment blueprints
        # or are instances that are part of the environment
        # XXX these might not be node_templates, need to check type
        for name, tpl in templates.items():
            # hacky way to exclude being part of the deployment plan and the manifest's status
            if virtual:
                directives = tpl.setdefault("directives", [])
                if "virtual" not in directives:
                    directives.append("virtual")
            node_templates[name] = tpl

    def _load_context(self, context, localEnv, include_all_imports):
        imports: List[dict] = context.get("imports") or []
        prefixes: Dict[str, list] = {}
        if imports and not include_all_imports:
            # only include imports that match a prefix required by a connection
            all_imports = list(imports)
            imports = []
            for imp_def in all_imports:
                prefix = imp_def.get("namespace_prefix")
                if prefix:
                    prefixes.setdefault(prefix + ".", []).append(imp_def)
                else:
                    imports.append(imp_def)
        connections = relabel_dict(context, localEnv, "connections")
        for name, c in connections.items():
            for prefix, imp_defs in prefixes.items():
                if c["type"].startswith(prefix):
                    imports.extend(imp_defs)
            if "default_for" not in c:
                c["default_for"] = "ANY"
            metadata = c.setdefault("metadata", {})
            metadata["from_environment"] = True
        if "primary_provider" not in connections:
            connections["_default_provider"] = dict(
                type="unfurl.relationships.ConnectsTo.ComputeMachines",
                default_for=RelationshipSpec.ANY,
            )
        tosca: Dict[str, Any] = dict(
            topology_template=dict(
                node_templates={}, relationship_templates=connections
            ),
        )
        if imports:
            tosca["imports"] = imports
        return tosca

    def load_external_ensemble(
        self, name: str, value: Dict[str, Any]
    ) -> List["RelationshipInstance"]:
        """
        :manifest: artifact template (file and optional repository name)
        :instance: "*" or name # default is root
        :schema: expected schema for attributes
        :url: uri of manifest
        """
        # load the manifest for the imported resource
        location = value.get("manifest")
        if not location:
            raise UnfurlError(
                f"Can not import external ensemble '{name}': no manifest specified"
            )

        if "project" in location:
            importedManifest = self.localEnv and self.localEnv.get_external_manifest(
                location, skip_validation=not self.validate, safe_mode=self.safe_mode
            )
            if not importedManifest:
                raise UnfurlError(
                    f"Can not import external ensemble '{name}': can't find project '{location['project']}'"
                )
            path = importedManifest.path
        else:
            # ensemble is in the same project
            baseDir = getattr(location, "base_dir", self.get_base_dir())
            artifact_tpl = dict(file=location["file"])
            if "repository" in location:
                artifact_tpl = location["repository"]
            artifact = ArtifactSpec(
                artifact_tpl,
                path=baseDir,
                topology=(self.tosca and self.tosca.topology)
                or ToscaSpec(get_default_topology()).topology,
            )
            path = artifact.get_path(self.get_import_resolver(expand=True))
            localEnv = LocalEnv(
                path,
                parent=self.localEnv,
                override_context=location.get("environment", ""),
            )
            if self.is_path_to_self(localEnv.manifestPath):
                # don't import self (might happen when context is shared)
                return []
            logger.verbose("loading external ensemble at %s", localEnv.manifestPath)
            importedManifest = localEnv.get_manifest(
                skip_validation=not self.validate, safe_mode=self.safe_mode
            )

        uri = value.get("uri")
        if uri and not importedManifest.has_uri(uri):
            raise UnfurlError(
                f"Error importing external ensemble at '{path}', uri mismatch for '{uri}'"
            )
        rname = value.get("instance", "root")
        if rname == "*":
            rname = "root"
        # use find_instance_or_external() not find_resource() to handle export instances transitively
        # e.g. to allow us to layer localhost manifests
        root = importedManifest.get_root_resource()
        resource = root and root.find_instance_or_external(rname)
        if not root or not resource:
            raise UnfurlError(
                f"Can not import external ensemble '{name}': instance '{rname}' not found"
            )
        version = value.get("version")
        if version:
            imported_version = importedManifest.version
            if not imported_version:
                raise UnfurlError(
                    f"Can not import external ensemble '{name}': requires version '{version}' and ensemble doesn't specify a version."
                )
            else:
                has_semver = is_semver(imported_version)
                if (not has_semver and imported_version != version) or (
                    has_semver
                    and not is_semver_compatible_with(version, imported_version)
                ):
                    raise UnfurlError(
                        f"Can not import external ensemble '{name}': ensemble's version '{imported_version}' isn't compatible with '{version}'"
                    )
        self.imports.add_import(name, resource, value)
        self._importedManifests[id(root)] = importedManifest
        matches = []
        connections = value.get("connections")
        if connections:
            for rel in root.default_relationships:
                if rel.source:
                    if f"{rel.source.name}::{rel.name}" in connections:
                        matches.append(rel)
                elif rel.name in connections:
                    matches.append(rel)
        return matches

    def load_changes(self, changes: Optional[List[dict]], changeLogPath: str) -> bool:
        if changes is not None:
            self.changeSets = {
                c.changeId: c
                for c in (self.load_config_change(changeSet) for changeSet in changes)
            }
        elif changeLogPath:
            fullLogPath = self.get_change_log_path()
            if os.path.isfile(fullLogPath):
                with open(fullLogPath) as f:
                    self.changeSets = {
                        c.changeId: c
                        for c in (
                            ChangeRecordRecord(parse=line.strip())
                            for line in f.readlines()
                            if not line.strip().startswith("#")
                        )
                        if not hasattr(c, "startCommit")  # not a job record
                    }
        return self.changeSets is not None

    def lfs_settings(self) -> Tuple[bool, bool, str, Optional[str]]:
        local = self.manifest.expanded.get("environment", {}).get("lfs_lock")
        env = self.context.get("lfs_lock", {}).copy()
        # give ensemble priority:
        if local:
            env.update(local)
        lock = cast(LfsSettings, env)
        enable = lock.get("lock", "no")
        assert enable in ("require", "no", "try")
        if not lock or enable == "no":
            return False, False, "", None
        else:
            lfs_try = True
            lfs_required = enable == "require"
            lfs_url = lock.get("url", "")
        lfs_lock_path = lock.get("name")
        if lfs_lock_path and self.localEnv:
            lock_vars = dict(
                environment=self.localEnv.manifest_context_name, ensemble_uri=self.uri
            )
            if self.repo:
                lock_vars["local_lock_path"] = os.path.relpath(
                    self.lockfilepath or "", self.repo.working_dir
                )
            lfs_lock_path = substitute_env(lfs_lock_path, lock_vars)
        elif self.lockfilepath and self.repo:
            lfs_lock_path = os.path.relpath(self.lockfilepath, self.repo.working_dir)
        else:
            lfs_lock_path = self.uri
        escaped_lfs_lock_path = encode_unfurl_identifier(lfs_lock_path, r"[^\w/-]")
        return lfs_try, lfs_required, escaped_lfs_lock_path, lfs_url

    def _lock_lfs(self) -> bool:
        lfs_try, lfs_required, lfs_lock_path, lfs_url = self.lfs_settings()
        if lfs_try:
            if self.repo and self.repo.is_lfs_enabled(lfs_url):
                if self.repo.lock_lfs(lfs_lock_path, lfs_url):
                    self.lfs_locked = lfs_lock_path
                    self.lfs_url = lfs_url
                    logger.debug(f"git lfs locked {self.lockfilepath}")
                    return True
                else:
                    msg = f"Ensemble {self.path} is remotely locked at {lfs_lock_path}"
                    raise UnfurlError(msg)
            elif lfs_required:
                msg = "git lfs is not available but is required to use this ensemble"
                raise UnfurlError(msg)
        return False

    def lock(self) -> bool:
        # implement simple local file locking -- no waiting on the lock
        # lock() should never be called when already holding a lock, raise error if it does
        msg = f"Ensemble {self.path} was already locked -- is there a circular reference between external ensembles?"
        if self.lockfile:
            raise UnfurlError(msg)
        if not self.lockfilepath:
            return False
        if os.path.exists(self.lockfilepath):
            with open(self.lockfilepath) as lf:
                pid = lf.read()
                if os.getpid() == int(pid):
                    raise UnfurlError(msg)
                else:
                    raise UnfurlError(
                        f"Lockfile '{self.lockfilepath}' already created by another process {pid} "
                    )
        else:
            # open exclusively, ok if we race here, we'll just raise an error
            self.lockfile = open(self.lockfilepath, "xb", buffering=0)
            self.lockfile.write(bytes(str(os.getpid()), "ascii"))  # type: ignore
            try:
                self._lock_lfs()
            except Exception:
                self.unlock()
                raise
            return True

    def unlock(self):
        if self.repo and self.lfs_locked:
            self.repo.unlock_lfs(self.lfs_locked, self.lfs_url)
            self.lfs_locked = None
        if self.lockfile and self.lockfilepath:
            # unlink first to avoid race (this will fail on Windows)
            os.unlink(self.lockfilepath)
            self.lockfile.close()
            self.lockfile = None
            return True
        return False

    def get_tosca_file_path(self) -> str:
        if not self.tosca:
            return ""
        assert self.tosca.template
        if self.repo:
            file_path = self.repo.find_path(self.tosca.template.path or "")[0] or ""
        else:
            file_path = self.tosca.template.path or ""
        if self.tosca.fragment:
            return file_path + "#" + self.tosca.fragment
        return file_path

    def find_last_operation(self, target, operation) -> Optional[ChangeRecordRecord]:
        if self._operationIndex is None:
            operationIndex: Dict[Tuple[str, str], str] = {}
            if self.changeSets:
                # add list() for 3.7
                for change in reversed(list(self.changeSets.values())):
                    if not change.target or not change.operation:
                        continue
                    key = (change.target, change.operation)
                    last = operationIndex.setdefault(key, change.changeId)
                    if last < change.changeId:
                        operationIndex[key] = change.changeId
            self._operationIndex = operationIndex
        changeId = self._operationIndex.get((target, operation))
        if changeId is not None and self.changeSets:
            return self.changeSets[changeId]
        return None

    def save_entity_instance(self, resource: EntityInstance) -> Tuple[str, Dict]:
        status = CommentedMap()
        status["template"] = resource.template.get_uri()

        # only save the attributes that were set by the instance, not spec properties or attribute defaults
        # particularly, because these will get loaded in later runs and mask any spec properties with the same name
        if resource._attributes:
            status["attributes"] = resource._attributes
        # save computed values for properties as they were observed
        if resource._properties:
            status["properties"] = resource._properties
        if resource.imported:
            status["imported"] = resource.imported
        save_status(resource, status)
        if resource.created is not None:
            status["created"] = resource.created
        if resource.protected is not None:
            status["protected"] = resource.protected
        if resource.customized is not None:
            status["customized"] = resource.customized
        return (resource.name, status)

    def save_artifact(self, resource: EntityInstance) -> Optional[Tuple[str, Dict]]:
        if resource.parent and resource.name not in resource.parent.template.artifacts:  # type: ignore
            name, status = self.save_entity_instance(resource)  # type: ignore
            # this artifact was dynamically added and is not part of the node template
            # so add the template spec inline
            status["template"] = resource.template.toscaEntityTemplate.entity_tpl
            return name, status
        else:
            return self._save_entity_if_instantiated(resource)

    def save_requirement(self, resource) -> Optional[Dict[str, Dict]]:
        if not resource.last_change and (
            not resource.local_status
            or resource.local_status <= Status.ok
            or resource.local_status == Status.pending
        ):
            # no reason to serialize requirements that haven't been instantiated
            return None
        name, status = self.save_entity_instance(resource)
        status["capability"] = resource.parent.key
        return {name: status}

    def _save_entity_if_instantiated(
        self, resource, checkstatus=True
    ) -> Optional[Tuple[str, Dict]]:
        try:
            if not self.is_instantiated(resource, checkstatus):
                # no reason to serialize entities that haven't been instantiated
                return None
            return self.save_entity_instance(resource)
        except Exception:
            logger.error(
                'Unexpected error saving "%s"', resource.nested_key, exc_info=True
            )
            return None

    def save_resource(
        self, resource: NodeInstance, discovered: Dict[str, Any]
    ) -> Optional[Tuple[str, Dict]]:
        # XXX checkstatus break unit tests so skip mostly
        checkstatus = (
            resource.template.type == "unfurl.nodes.LocalRepository"
            or "default" in resource.template.directives
            or resource.template.toscaEntityTemplate.is_replaced_by_outer()
        )
        ret = self._save_entity_if_instantiated(resource, checkstatus)
        if not ret:
            return ret
        name, status = ret

        if (
            self.tosca
            and self.tosca.discovered
            and resource.template.nested_name in self.tosca.discovered
        ):
            discovered[resource.template.nested_name] = self.tosca.discovered[
                resource.template.nested_name
            ]

        if resource._capabilities:
            capabilities = list(
                filter(
                    None,
                    map(self._save_entity_if_instantiated, resource.capabilities),
                )
            )
            if capabilities:
                status["capabilities"] = CommentedMap(capabilities)

        if resource._requirements:
            requirements = list(
                filter(None, map(self.save_requirement, resource.requirements))
            )
            if requirements:
                status["requirements"] = requirements

        if resource._artifacts:
            # assumes names are unique!
            artifacts = list(filter(None, map(self.save_artifact, resource._artifacts)))
            if artifacts:
                status["artifacts"] = CommentedMap(artifacts)

        if cast(NodeSpec, resource.template).substitution and resource.shadow:
            assert resource.shadow.root is not resource.root, (
                resource is resource.shadow,
                resource.root,
            )
            status["substitution"] = self.save_root_resource(
                cast(TopologyInstance, resource.shadow.root), discovered
            )

        if resource.instances:
            status["instances"] = CommentedMap(
                filter(
                    None,
                    map(
                        lambda r: self.save_resource(r, discovered),  # type: ignore
                        resource.instances,
                    ),
                )
            )
        return (name, status)

    def save_root_resource(
        self, root: TopologyInstance, discovered: Dict[str, Any]
    ) -> CommentedMap:
        status = CommentedMap()
        # record the input and output values
        status["inputs"] = serialize_value(root.attributes["inputs"])
        status["outputs"] = serialize_value(root.attributes["outputs"])

        save_status(root, status)
        status["instances"] = CommentedMap(
            filter(
                None,
                map(
                    lambda r: self.save_resource(r, discovered),
                    cast(Iterable[NodeInstance], root.get_operational_dependencies()),
                ),
            )
        )
        return status

    def save_job_record(self, job: "Job") -> CommentedMap:
        """
        .. code-block:: YAML

          changeId: 1
          startCommit: # commit when job began
          startTime:
          workflow:
          options: # job options set by the user
          summary:
          specDigest:
          endCommit:   # commit updating status (only appears in changelog file)
        """
        output = CommentedMap()
        output["changeId"] = job.changeId
        output["startTime"] = job.get_start_time()
        output["endTime"] = job.get_end_time()
        if job.previousId:
            output["previousId"] = job.previousId
        if job.jobOptions.masterJob:
            master_job = job.jobOptions.masterJob
            # if this was run by another ensemble's job
            masterJob = CommentedMap()
            masterJob["path"] = master_job.manifest.path
            masterJob["changeId"] = master_job.changeId
            if master_job.manifest.currentCommitId:
                masterJob["startCommit"] = master_job.manifest.currentCommitId
            output["masterJob"] = masterJob
        options = job.jobOptions.get_user_settings()
        output["workflow"] = options.pop("workflow", Defaults.workflow)
        output["options"] = options
        output["summary"] = job.stats(asMessage=True)
        if self.currentCommitId:
            output["startCommit"] = self.currentCommitId
        output["specDigest"] = self.specDigest
        return save_status(job, output)

    def save_job(self, job: "Job") -> Tuple[CommentedMap, List[CommentedMap]]:
        discovered = CommentedMap()
        assert self.rootResource
        changed = self.save_root_resource(self.rootResource, discovered)

        # update changed with includes, this may change objects with references to these objects
        self.manifest.restore_includes(changed)
        assert self.manifest.config
        # only saved discovered templates that are still referenced
        spec = self.manifest.config.setdefault("spec", {})
        spec.pop("discovered", None)
        if discovered:
            spec["discovered"] = discovered

        # modify original to preserve structure and comments
        lock = Lock(self).lock()
        if "lock" not in self.manifest.config:
            self.manifest.config["lock"] = {}
        if not self.manifest.config["lock"]:
            self.manifest.config["lock"] = lock
        else:
            patch_dict(self.manifest.config["lock"], lock)

        # modify original to preserve structure and comments
        if "status" not in self.manifest.config:
            self.manifest.config["status"] = {}
        if not self.manifest.config["status"]:
            self.manifest.config["status"] = changed
        else:
            patch_dict(self.manifest.config["status"], changed)

        jobRecord = self.save_job_record(job)
        if job.workDone:
            self.manifest.config["lastJob"] = jobRecord
            # don't save result.results into this yaml, it might contain sensitive data
            exclude_result = not self.changeLogPath and not job.dry_run
            changes = list(
                map(lambda t: save_task(t, exclude_result), job.workDone.values())
            )
            if self.changeLogPath and self.path is not None:
                self.manifest.config["jobsLog"] = self.changeLogPath  # jobs.tsv

                jobLogPath = job.log_path("changes", ".yaml")
                jobLogRelPath = os.path.relpath(jobLogPath, os.path.dirname(self.path))
                jobRecord["changelog"] = jobLogRelPath
            else:
                self.manifest.config.setdefault("changes", []).extend(changes)
        else:
            # no work was done
            changes = []

        output = job.out or job.jobOptions.out  # type: ignore
        if output:
            if job.dry_run:
                logger.info("printing results from dry run")
            self.dump(output)
        else:
            job.out = self.manifest.save()  # type: ignore
        return jobRecord, changes

    def commit_job(self, job: "Job") -> None:
        if job.jobOptions.planOnly:
            return
        if job.dry_run and job.jobOptions.skip_save != "never":
            if not job.jobOptions.out and self.manifest.path:  # type: ignore
                job.jobOptions.out = sys.stdout  # type: ignore
        jobRecord, changes = self.save_job(job)
        if not changes:
            logger.info("job run didn't make any changes; nothing to commit")
            return

        if self.changeLogPath:
            if job.dry_run:  # don't commit dry run changes
                self.save_change_log(
                    job.log_path(ext=".yaml"), jobRecord, changes, cleartext_yaml
                )
            else:
                local_changes, committed_changes = split_changes(changes)
                self.save_change_log(
                    job.log_path(ext=".yaml"), jobRecord, local_changes, cleartext_yaml
                )
                jobLogPath = job.log_path("changes", ".yaml")
                self.save_change_log(jobLogPath, jobRecord, committed_changes)
                self._append_log(job, jobRecord, changes, jobLogPath)

        if job.dry_run:
            return

        if job.jobOptions.commit and self.repo:
            if job.jobOptions.message is not None:
                message = job.jobOptions.message
            else:
                message = self.get_default_commit_message()

            # only commit the ensemble repository:
            ensembleRepo = self.repositories["self"]
            if ensembleRepo.is_dirty():
                ensembleRepo.commit(message, True)
                if job.jobOptions.push and ensembleRepo.repo:
                    ensembleRepo.repo.push()

    def get_default_commit_message(self):
        jobRecord = self.manifest.config.get("lastJob")
        if jobRecord:
            return f"Updating status for job {jobRecord['changeId']}"
        else:
            return "Commit by Unfurl"

    def get_repo_status(self, dirty=False) -> str:
        return "".join([r.get_repo_status(dirty) for r in self.repositories.values()])

    def add_all(self) -> None:
        for repository in self.repositories.values():
            if not repository.read_only and repository.is_dirty():
                repository.add_all()

    def save_secrets(self) -> List[Path]:
        saved: List[Path] = []
        for repository in self.repositories.values():
            if not repository.read_only:
                saved.extend(repository.save_secrets())
        return saved

    def commit(
        self, msg: str, add_all: bool = False, save_secrets=True, *, ensemble_only=False
    ) -> int:
        committed = 0
        save_secrets = save_secrets and (
            not self.localEnv or not self.localEnv.overrides.get("skip_secret_files")
        )
        if not ensemble_only:
            for repository in self.repositories.values():
                if repository.repo == self.repo:
                    continue
                if not repository.read_only and repository.is_dirty():
                    retVal = repository.commit(msg, add_all, save_secrets)
                    committed += 1
                    logger.info(
                        "committed %s to %s: %s", retVal, repository.working_dir, msg
                    )
        # if manifest was changed: # e.g. calling commit after a job was run
        #    if commits were made writeLock and save updated manifest??
        #    (note: endCommit will be omitted as changes.yaml isn't updated)
        ensembleRepo = self.repositories["self"]
        if ensembleRepo.is_dirty():
            retVal = ensembleRepo.commit(msg, add_all, save_secrets)
            committed += 1
            logger.info("committed %s to %s: %s", retVal, ensembleRepo.working_dir, msg)

        return committed

    def get_change_log_path(self) -> str:
        # jobs.tsv
        return os.path.join(
            self.get_base_dir(), self.changeLogPath or DefaultNames.JobsLog
        )

    def get_job_log_path(self, startTime, folder_name, ext=".yaml") -> str:
        name = os.path.basename(self.get_change_log_path())
        # try to figure out any custom name pattern from changelogPath:
        defaultName = os.path.splitext(DefaultNames.JobsLog)[0]
        currentName = os.path.splitext(name)[0]
        prefix, _, suffix = currentName.partition(defaultName)
        fileName = prefix + "job" + startTime + suffix + ext
        return os.path.join(self.get_base_dir(), folder_name, fileName)

    def _append_log(self, job, jobRecord, changes, jobLogPath):
        logPath = self.get_change_log_path()
        jobLogRelPath = os.path.relpath(jobLogPath, os.path.dirname(logPath))
        if not os.path.isdir(os.path.dirname(logPath)):
            os.makedirs(os.path.dirname(logPath))
        logger.info("saving changelog to %s", logPath)
        with open(logPath, "a") as f:
            attrs = dict(status=job.status.name)
            attrs.update({
                k: jobRecord[k]
                for k in (
                    "status",
                    "startTime",
                    "specDigest",
                    "startCommit",
                    "summary",
                )
                if k in jobRecord
            })
            attrs["changelog"] = jobLogRelPath
            f.write(job.log(attrs))

            for change in changes:
                if "readyState" not in change:
                    continue  # never ran (skipped)
                status = change["readyState"].get("effective") or change[
                    "readyState"
                ].get("local")
                attrs = dict(
                    previousId=change.get("previousId", ""),
                    status=status,
                    target=change["target"],
                    operation=change["implementation"]["operation"],
                )
                for key in change.keys():
                    if key.startswith("digest"):
                        attrs[key] = change[key]
                attrs["summary"] = change["summary"]
                line = ChangeRecordRecord.format_log(change["changeId"], attrs)
                f.write(line)

    def save_change_log(self, fullPath, jobRecord, newChanges, yaml=None) -> None:
        try:
            changelog = CommentedMap()
            if self.manifest.path is not None:
                changelog["manifest"] = os.path.relpath(
                    self.manifest.path, os.path.dirname(fullPath)
                )
            changes = itertools.chain([jobRecord], newChanges)
            changelog["changes"] = list(changes)
            output = io.StringIO()
            (yaml or self.yaml).dump(changelog, output)
            if not os.path.isdir(os.path.dirname(fullPath)):
                os.makedirs(os.path.dirname(fullPath))
            logger.info("saving job changes to %s", fullPath)
            with open(fullPath, "w") as f:
                f.write(output.getvalue())
        except Exception:
            raise UnfurlError(f"Error saving changelog {self.changeLogPath}", True)
