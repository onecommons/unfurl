# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
"""Loads and saves a ensemble manifest.
"""
from __future__ import absolute_import
import six
import sys
from collections.abc import MutableSequence, Mapping
import numbers
import os
import os.path
import itertools

from . import DefaultNames
from .util import UnfurlError, to_yaml_text, filter_env
from .merge import patch_dict, intersect_dict
from .yamlloader import YamlConfig, make_yaml
from .result import serialize_value, ChangeRecord
from .support import ResourceChanges, Defaults, Imports, Status
from .localenv import LocalEnv
from .lock import Lock
from .manifest import Manifest
from .tosca import ArtifactSpec
from .runtime import TopologyInstance
from .eval import map_value
from .planrequests import create_instance_from_spec

from ruamel.yaml.comments import CommentedMap
from codecs import open
from ansible.parsing.dataloader import DataLoader

import logging

logger = logging.getLogger("unfurl")

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
    return saved


def save_resource_changes(changes):
    d = CommentedMap()
    for k, v in changes.items():
        # k is the resource key
        d[k] = serialize_value(v[ResourceChanges.attributesIndex] or {})
        if v[ResourceChanges.statusIndex] is not None:
            d[k][".status"] = v[ResourceChanges.statusIndex].name
        if v[ResourceChanges.addedIndex]:
            d[k][".added"] = serialize_value(v[ResourceChanges.addedIndex])
    return d


def has_status(operational):
    return operational.last_change or operational.status


def save_status(operational, status=None):
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
    if operational.priority:  # and operational.priority != Defaults.shouldRun:
        status["priority"] = operational.priority.name
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


def save_task(task):
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
    if task._resolved_inputs:  # only serialize resolved inputs
        output["inputs"] = serialize_value(task._resolved_inputs)
    changes = save_resource_changes(task._resourceChanges)
    if changes:
        output["changes"] = changes
    if task.messages:
        output["messages"] = task.messages
    dependencies = [save_dependency(val) for val in task.dependencies]
    if dependencies:
        output["dependencies"] = dependencies
    if task.result:
        if task.result.outputs:
            output["outputs"] = save_result(task.result.outputs)
        if task.result.result:
            output["result"] = save_result(task.result.result)
    else:
        output["result"] = "skipped"
    output.update(task.configurator.save_digest(task))
    output["summary"] = task.summary()
    return output


def relabel_dict(context, localEnv, key):
    connections = context.get(key)
    if not connections:
        return {}
    contexts = {}
    if localEnv:
        project = localEnv.project or localEnv.homeProject
        if project:
            contexts = project.contexts

    # handle items like newname : oldname to rename merged connections
    def follow_alias(v):
        if isinstance(v, str):
            env, sep, name = v.partition(":")
            if sep:  # found a ":"
                v = contexts[env][key][name]
            else:  # look in current dict
                v = connections[env]
            return follow_alias(v)  # follow
        else:
            return v

    return dict((n, follow_alias(v)) for n, v in connections.items())


class ReadOnlyManifest(Manifest):
    """Loads an ensemble from a manifest but doesn't instantiate the instance model."""

    def __init__(
        self, manifest=None, path=None, validate=True, localEnv=None, vault=None
    ):
        path = path or localEnv and localEnv.manifestPath
        if path:
            path = os.path.abspath(path)
        super().__init__(path, localEnv)
        self.manifest = YamlConfig(
            manifest,
            self.path,
            validate,
            os.path.join(_basepath, "manifest-schema.json"),
            self.load_yaml_include,
            vault,
        )
        if self.manifest.path:
            logger.debug("loaded ensemble manifest at %s", self.manifest.path)
        manifest = self.manifest.expanded
        spec = manifest.get("spec", {})
        self.context = manifest.get("environment", CommentedMap())
        if localEnv:
            self.context = localEnv.get_context(self.context)
        spec["inputs"] = self.context.get("inputs", spec.get("inputs", {}))
        self.update_repositories(
            manifest, relabel_dict(self.context, localEnv, "repositories")
        )

    @property
    def uris(self):
        uris = []
        if "metadata" in self.manifest.config:
            uri = self.metadata.get("uri")
            uris = self.metadata.get("aliases") or []
            if uri:
                return [uri] + uris
        return uris

    @property
    def uri(self):
        uris = self.uris
        if uris:
            return uris[0]
        else:
            return ""

    def has_uri(self, uri):
        return uri in self.uris

    @property
    def metadata(self):
        return self.manifest.config.setdefault("metadata", CommentedMap())

    @property
    def yaml(self):
        return self.manifest.yaml

    def get_base_dir(self):
        return self.manifest.get_base_dir()

    def is_path_to_self(self, path):
        if self.path is None or path is None:
            return False
        return os.path.abspath(self.path) == os.path.abspath(path)

    # def addRepo(self, name, repo):
    #     self._getRepositories(self.manifest.config)[name] = repo

    def dump(self, out=sys.stdout):
        self.manifest.dump(out)


def clone(localEnv, destPath):
    clone = ReadOnlyManifest(localEnv=localEnv)
    config = clone.manifest.config
    for key in ["status", "changes", "lastJob"]:
        config.pop(key, None)
    if "metadata" in config:
        config["metadata"].pop("uri", None)
        config["metadata"].pop("aliases", None)
    repositories = Manifest._get_repositories(config)
    repositories.pop("self", None)
    clone.path = destPath
    clone.manifest.path = destPath
    return clone


class YamlManifest(ReadOnlyManifest):
    _operationIndex = None
    lockfilepath = None
    lockfile = None

    def __init__(
        self, manifest=None, path=None, validate=True, localEnv=None, vault=None
    ):
        super().__init__(manifest, path, validate, localEnv, vault)
        # instantiate the tosca template
        manifest = self.manifest.expanded
        if self.manifest.path:
            self.lockfilepath = self.manifest.path + ".lock"

        more_spec = self._load_context(self.context, localEnv)
        self._set_spec(manifest, more_spec)
        assert self.tosca
        spec = manifest.get("spec", {})
        status = manifest.get("status", {})

        self.changeLogPath = manifest.get("jobsLog")
        self.jobsFolder = manifest.get("jobsFolder", "jobs")
        if not self.changeLogPath and localEnv and manifest.get("changes") is None:
            # save changes to a separate file if we're in a local environment
            self.changeLogPath = DefaultNames.JobsLog
        self.load_changes(manifest.get("changes"), self.changeLogPath)

        self.lastJob = manifest.get("lastJob")

        self.imports = Imports()
        self._importedManifests = {}

        if localEnv:
            for name in ["locals", "secrets"]:
                self.imports[name.rstrip("s")] = localEnv.get_local_instance(
                    name, self.context
                )

        rootResource = self.create_topology_instance(status)

        # create an new instances declared in the spec:
        for name, instance in spec.get("instances", {}).items():
            if not rootResource.find_resource(name):
                if "readyState" not in instance:
                    instance["readyState"] = "ok"
                create_instance_from_spec(self, rootResource, name, instance)

        self._configure_root(rootResource)
        self._ready(rootResource)

    def _configure_root(self, rootResource):
        rootResource.imports = self.imports
        if (
            self.manifest.vault and self.manifest.vault.secrets
        ):  # setBaseDir() may create a new templar
            rootResource._templar._loader.set_vault_secrets(self.manifest.vault.secrets)
        rootResource.envRules = self.context.get("variables") or CommentedMap()
        if not self.localEnv:
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

    def create_topology_instance(self, status):
        """
        If an instance of the toplogy is recorded in status, load it,
        otherwise create a new resource using the the topology as its template
        """
        # XXX use the substitution_mapping (3.8.12) represent the resource
        template = self.tosca.topology
        operational = self.load_status(status)
        root = TopologyInstance(template, operational)
        if os.environ.get("UNFURL_WORKDIR"):
            root.set_base_dir(os.environ["UNFURL_WORKDIR"])
        elif not self.path:
            root.set_base_dir(root.tmp_dir)
        else:
            root.set_base_dir(self.get_base_dir())

        # We need to set the environment as early as possible but not too early
        # and only once.
        # Now that we loaded the main manifest and set the root's baseDir
        # let's do it before we import any other manifests.
        # But only if we're the main manifest.
        if not self.localEnv or not self.localEnv.parent:
            if self.context.get("variables"):
                env = filter_env(map_value(self.context["variables"], root))
            else:
                env = os.environ.copy()
            for rel in root.requirements:
                t = lambda datatype: datatype.type == "unfurl.datatypes.EnvVar"
                env.update(rel.merge_props(t, True))
            intersect_dict(os.environ, env)  # remove keys not in env
            os.environ.update(env)
            paths = self.localEnv and self.localEnv.get_paths()
            if paths:
                os.environ["PATH"] = (
                    os.pathsep.join(paths) + os.pathsep + os.environ.get("PATH", [])
                )
                logger.debug("PATH set to %s", os.environ["PATH"])

        # self.load_external_ensemble("localhost", tpl)
        importsSpec = self.context.get("external", {})
        # note: external "localhost" is defined in UNFURL_HOME's context by convention
        for name, value in importsSpec.items():
            self.load_external_ensemble(name, value)

        # need to set rootResource before createNodeInstance() is called
        self.rootResource = root
        for key, val in status.get("instances", {}).items():
            self.create_node_instance(key, val, root)
        return root

    def _load_context(self, context, localEnv):
        imports = context.get("imports")
        connections = relabel_dict(context, localEnv, "connections")
        for name, c in connections.items():
            if "default_for" not in c:
                c["default_for"] = "ANY"
        tosca = dict(
            topology_template=dict(
                node_templates={}, relationship_templates=connections
            ),
        )
        if imports:
            tosca["imports"] = imports
        return tosca

    def load_external_ensemble(self, name, value):
        """
        :manifest: artifact template (file and optional repository name)
        :instance: "*" or name # default is root
        :schema: # expected schema for attributes
        """
        # load the manifest for the imported resource
        location = value.get("manifest")
        if not location:
            raise UnfurlError(
                f"Can not import external ensemble '{(name)}': no manifest specified"
            )

        if "project" in location:
            importedManifest = self.localEnv.get_external_manifest(location)
            if not importedManifest:
                raise UnfurlError(
                    f"Can not import external ensemble '{name}': can't find project '{location['project']}'"
                )
        else:
            # ensemble is in the same project
            baseDir = getattr(location, "base_dir", self.get_base_dir())
            artifact = ArtifactSpec(location, path=baseDir, spec=self.tosca)
            path = artifact.get_path()
            localEnv = LocalEnv(path, parent=self.localEnv)
            if self.path and os.path.abspath(self.path) == os.path.abspath(
                localEnv.manifestPath
            ):
                # don't import self (might happen when context is shared)
                return
            importedManifest = localEnv.get_manifest()

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
        resource = root.find_instance_or_external(rname)
        if not resource:
            raise UnfurlError(
                f"Can not import external ensemble '{name}': instance '{rname}' not found"
            )
        self.imports[name] = (resource, value)
        self._importedManifests[id(root)] = importedManifest

    def load_changes(self, changes, changeLogPath):
        # self.changeSets[changeid => ChangeRecords]
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
                            ChangeRecord(parse=line.strip())
                            for line in f.readlines()
                            if not line.strip().startswith("#")
                        )
                        if not hasattr(c, "startCommit")  # not a job record
                    }
        return self.changeSets is not None

    def lock(self):
        # implement simple local file locking -- no waiting on the lock
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
            # ok if we race here, we'll just raise an error
            self.lockfile = open(self.lockfilepath, "xb", buffering=0)
            self.lockfile.write(bytes(str(os.getpid()), "ascii"))
            return True

    def unlock(self):
        if self.lockfile:
            # unlink first to avoid race (this will fail on Windows)
            os.unlink(self.lockfilepath)
            self.lockfile.close()
            self.lockfile = None
            return True
        return False

    def find_last_operation(self, target, operation):
        if self._operationIndex is None:
            operationIndex = {}
            if self.changeSets:
                # add list() for 3.7
                for change in reversed(list(self.changeSets.values())):
                    if not hasattr(change, "target") or not hasattr(
                        change, "operation"
                    ):
                        continue
                    key = (change.target, change.operation)
                    last = operationIndex.setdefault(key, change.changeId)
                    if last < change.changeId:
                        operationIndex[key] = change
            self._operationIndex = operationIndex
        changeId = self._operationIndex.get((target, operation))
        if changeId is not None:
            return self.changeSets[changeId]
        return None

    def save_entity_instance(self, resource):
        status = CommentedMap()
        status["template"] = resource.template.get_uri()

        # only save the attributes that were set by the instance, not spec properties or attribute defaults
        # particularly, because these will get loaded in later runs and mask any spec properties with the same name
        if resource._attributes:
            status["attributes"] = resource._attributes
        if resource.shadow:
            # name will be the same as the import name
            status["imported"] = resource.name
        save_status(resource, status)
        if resource.created is not None:
            status["created"] = resource.created

        return (resource.name, status)

    def save_requirement(self, resource):
        if not resource.last_change and (
            not resource.local_status or resource.local_status <= Status.ok
        ):
            # no reason to serialize requirements that haven't been instantiated
            return None
        name, status = self.save_entity_instance(resource)
        status["capability"] = resource.parent.key
        return [{name: status}]

    def _save_entity_if_instantiated(self, resource, checkstatus=True):
        if not resource.last_change and (
            not resource.local_status
            or (
                checkstatus
                and resource.local_status in [Status.unknown, Status.ok, Status.pending]
            )
        ):
            # no reason to serialize entities that haven't been instantiated
            return None
        return self.save_entity_instance(resource)

    def save_resource(self, resource, discovered):
        # XXX checkstatus break unit tests:
        ret = self._save_entity_if_instantiated(resource, False)
        if not ret:
            return ret
        name, status = ret

        if self.tosca.discovered and resource.template.name in self.tosca.discovered:
            discovered[resource.template.name] = self.tosca.discovered[
                resource.template.name
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
            artifacts = list(
                filter(
                    None, map(self._save_entity_if_instantiated, resource._artifacts)
                )
            )
            if artifacts:
                status["artifacts"] = CommentedMap(artifacts)

        if resource.instances:
            status["instances"] = CommentedMap(
                filter(
                    None,
                    map(
                        lambda r: self.save_resource(r, discovered), resource.instances
                    ),
                )
            )

        return (name, status)

    def save_root_resource(self, discovered):
        resource = self.rootResource
        status = CommentedMap()

        # record the input and output values
        status["inputs"] = serialize_value(resource.attributes["inputs"])
        status["outputs"] = serialize_value(resource.attributes["outputs"])

        save_status(resource, status)
        # getOperationalDependencies() skips inputs and outputs
        status["instances"] = CommentedMap(
            filter(
                None,
                map(
                    lambda r: self.save_resource(r, discovered),
                    resource.get_operational_dependencies(),
                ),
            )
        )
        return status

    def save_job_record(self, job):
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
        if job.previousId:
            output["previousId"] = job.previousId
        if job.masterJob:
            # if this was run by another ensemble's job
            masterJob = CommentedMap()
            masterJob["path"] = job.masterJob.manifest.path
            masterJob["changeId"] = job.masterJob.changeId
            if job.masterJob.manifest.currentCommitId:
                masterJob["startCommit"] = job.masterJob.manifest.currentCommitId
            output["masterJob"] = masterJob
        options = job.jobOptions.get_user_settings()
        output["workflow"] = options.pop("workflow", Defaults.workflow)
        output["options"] = options
        output["summary"] = job.stats(asMessage=True)
        if self.currentCommitId:
            output["startCommit"] = self.currentCommitId
        output["specDigest"] = self.specDigest
        return save_status(job, output)

    def save_job(self, job):
        discovered = CommentedMap()
        changed = self.save_root_resource(discovered)

        # update changed with includes, this may change objects with references to these objects
        self.manifest.restore_includes(changed)
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
            changes = list(map(save_task, job.workDone.values()))
            if self.changeLogPath:
                self.manifest.config["jobsLog"] = self.changeLogPath

                jobLogPath = self.get_job_log_path(jobRecord["startTime"])
                jobLogRelPath = os.path.relpath(jobLogPath, os.path.dirname(self.path))
                jobRecord["changelog"] = jobLogRelPath
            else:
                self.manifest.config.setdefault("changes", []).extend(changes)
        else:
            # no work was done
            changes = []

        if job.out:
            self.dump(job.out)
        else:
            job.out = self.manifest.save()
        return jobRecord, changes

    def commit_job(self, job):
        if job.planOnly:
            return
        if job.dry_run:
            logger.info("printing results from dry run")
            if not job.out and self.manifest.path:
                job.out = sys.stdout
        jobRecord, changes = self.save_job(job)
        if not changes:
            logger.info("job run didn't make any changes; nothing to commit")
            return

        if self.changeLogPath:
            jobLogPath = self.save_change_log(jobRecord, changes)
            if not job.dry_run:
                self._append_log(job, jobRecord, changes, jobLogPath)

        if job.dry_run:
            return

        if job.commit and self.repo:
            if job.message is not None:
                message = job.message
            else:
                message = self.get_default_commit_message()
            self.commit(message, True)

    def get_default_commit_message(self):
        jobRecord = self.manifest.config.get("lastJob")
        if jobRecord:
            return f"Updating status for job {jobRecord['changeId']}"
        else:
            return "Commit by Unfurl"

    def get_repo_status(self, dirty=False):
        return "".join([r.get_repo_status(dirty) for r in self.repositories.values()])

    def add_all(self):
        for repository in self.repositories.values():
            if not repository.readOnly and repository.is_dirty():
                repository.add_all()

    def commit(self, msg, addAll):
        committed = 0
        for repository in self.repositories.values():
            if repository.repo == self.repo:
                continue
            if not repository.readOnly and repository.is_dirty():
                retVal = repository.commit(msg, addAll)
                committed += 1
                logger.info(
                    "committed %s to %s: %s", retVal, repository.working_dir, msg
                )
        # if manifest was changed: # e.g. calling commit after a job was run
        #    if commits were made writeLock and save updated manifest??
        #    (note: endCommit will be omitted as changes.yaml isn't updated)
        ensembleRepo = self.repositories["self"]
        if ensembleRepo.is_dirty():
            retVal = ensembleRepo.commit(msg, addAll)
            committed += 1
            logger.info("committed %s to %s: %s", retVal, ensembleRepo.working_dir, msg)

        return committed

    def get_change_log_path(self):
        return os.path.join(
            self.get_base_dir(), self.changeLogPath or DefaultNames.JobsLog
        )

    def get_job_log_path(self, startTime, ext=".yaml"):
        name = os.path.basename(self.get_change_log_path())
        # try to figure out any custom name pattern from changelogPath:
        defaultName = os.path.splitext(DefaultNames.JobsLog)[0]
        currentName = os.path.splitext(name)[0]
        prefix, _, suffix = currentName.partition(defaultName)
        fileName = prefix + "job" + startTime + suffix + ext
        return os.path.join(self.get_base_dir(), self.jobsFolder, fileName)

    def _append_log(self, job, jobRecord, changes, jobLogPath):
        logPath = self.get_change_log_path()
        jobLogRelPath = os.path.relpath(jobLogPath, os.path.dirname(logPath))
        if not os.path.isdir(os.path.dirname(logPath)):
            os.makedirs(os.path.dirname(logPath))
        logger.info("saving changelog to %s", logPath)
        with open(logPath, "a") as f:
            attrs = dict(status=job.status.name)
            attrs.update(
                {
                    k: jobRecord[k]
                    for k in (
                        "status",
                        "startTime",
                        "specDigest",
                        "startCommit",
                        "summary",
                    )
                    if k in jobRecord
                }
            )
            attrs["changelog"] = jobLogRelPath
            f.write(job.log(attrs))

            for change in changes:
                status = change["readyState"]
                attrs = dict(
                    previousId=change.get("previousId", ""),
                    status=status.get("effective") or status.get("local"),
                    target=change["target"],
                    operation=change["implementation"]["operation"],
                )
                for key in change.keys():
                    if key.startswith("digest"):
                        attrs[key] = change[key]
                attrs["summary"] = change["summary"]
                line = ChangeRecord.format_log(change["changeId"], attrs)
                f.write(line)

    def save_change_log(self, jobRecord, newChanges):
        try:
            changelog = CommentedMap()
            fullPath = self.get_job_log_path(jobRecord["startTime"])
            changelog["manifest"] = os.path.relpath(
                self.manifest.path, os.path.dirname(fullPath)
            )
            changes = itertools.chain([jobRecord], newChanges)
            changelog["changes"] = list(changes)
            output = six.StringIO()
            self.yaml.dump(changelog, output)
            if not os.path.isdir(os.path.dirname(fullPath)):
                os.makedirs(os.path.dirname(fullPath))
            logger.info("saving job changes to %s", fullPath)
            with open(fullPath, "w") as f:
                f.write(output.getvalue())
            return fullPath
        except:
            raise UnfurlError(f"Error saving changelog {self.changeLogPath}", True)
