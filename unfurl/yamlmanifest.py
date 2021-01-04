# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
"""Loads and saves a ensemble manifest with the following format
"""
from __future__ import absolute_import
import six
import sys
import collections
import numbers
import os.path
import itertools

from . import DefaultNames
from .util import UnfurlError, toYamlText, filterEnv
from .merge import patchDict, intersectDict
from .yamlloader import YamlConfig
from .result import serializeValue
from .support import ResourceChanges, Defaults, Imports, Status
from .localenv import LocalEnv
from .lock import Lock
from .manifest import Manifest
from .tosca import Artifact
from .runtime import TopologyInstance
from .eval import mapValue
from .tosca import ToscaSpec, TOSCA_VERSION

from ruamel.yaml.comments import CommentedMap
from codecs import open

import logging

logger = logging.getLogger("unfurl")

_basepath = os.path.abspath(os.path.dirname(__file__))


def saveConfigSpec(spec):
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


def saveDependency(dep):
    saved = CommentedMap()
    if dep.name:
        saved["name"] = dep.name
    saved["ref"] = dep.expr
    if dep.expected is not None:
        saved["expected"] = serializeValue(dep.expected)
    if dep.schema is not None:
        saved["schema"] = dep.schema
    if dep.required:
        saved["required"] = dep.required
    if dep.wantList:
        saved["wantList"] = dep.wantList
    return saved


def saveResourceChanges(changes):
    d = CommentedMap()
    for k, v in changes.items():
        # k is the resource key
        d[k] = serializeValue(v[ResourceChanges.attributesIndex] or {})
        if v[ResourceChanges.statusIndex] is not None:
            d[k][".status"] = v[ResourceChanges.statusIndex].name
        if v[ResourceChanges.addedIndex]:
            d[k][".added"] = serializeValue(v[ResourceChanges.addedIndex])
    return d


def hasStatus(operational):
    return operational.lastChange or operational.status


def saveStatus(operational, status=None):
    if status is None:
        status = CommentedMap()
    if not hasStatus(operational):
        # skip status
        return status

    readyState = CommentedMap()
    if operational.localStatus is not None:
        if operational.status != operational.localStatus:
            # if different serialize this too
            readyState["effective"] = operational.status.name
        readyState["local"] = operational.localStatus.name
    else:
        readyState["effective"] = operational.status.name
    if operational.state is not None:
        readyState["state"] = operational.state.name
    if operational.priority:  # and operational.priority != Defaults.shouldRun:
        status["priority"] = operational.priority.name
    status["readyState"] = readyState

    if operational.lastStateChange:
        status["lastStateChange"] = operational.lastStateChange
    if operational.lastConfigChange:
        status["lastConfigChange"] = operational.lastConfigChange

    return status


def saveResult(value):
    if isinstance(value, collections.Mapping):
        return CommentedMap(
            (key, saveResult(v)) for key, v in value.items() if v is not None
        )
    elif isinstance(value, (collections.MutableSequence, tuple)):
        return [saveResult(item) for item in value]
    elif value is not None and not isinstance(value, (numbers.Real, bool)):
        return toYamlText(value)
    else:
        return value


def saveTask(task):
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
    if task.target:
        output["target"] = task.target.key
    saveStatus(task, output)
    output["implementation"] = saveConfigSpec(task.configSpec)
    if task._inputs:  # only serialize resolved inputs
        output["inputs"] = task.inputs.serializeResolved()
    changes = saveResourceChanges(task._resourceChanges)
    if changes:
        output["changes"] = changes
    if task.messages:
        output["messages"] = task.messages
    dependencies = [saveDependency(val) for val in task.dependencies.values()]
    if dependencies:
        output["dependencies"] = dependencies
    if task.result:
        if task.result.outputs:
            output["outputs"] = saveResult(task.result.outputs)
        if task.result.result:
            output["result"] = saveResult(task.result.result)
    else:
        output["result"] = "skipped"

    return output


class ReadOnlyManifest(Manifest):
    """Loads an ensemble from a manifest but doesn't instantiate the instance model."""

    def __init__(
        self, manifest=None, path=None, validate=True, localEnv=None, vault=None
    ):
        assert not (localEnv and (manifest or path))  # invalid combination of args
        path = path or localEnv and localEnv.manifestPath
        if path:
            path = os.path.abspath(path)
        super(ReadOnlyManifest, self).__init__(path, localEnv)
        self.manifest = YamlConfig(
            manifest,
            self.path,
            validate,
            os.path.join(_basepath, "manifest-schema.json"),
            self.loadYamlInclude,
            vault,
        )
        if self.manifest.path:
            logging.debug("loaded ensemble manifest at %s", self.manifest.path)
        manifest = self.manifest.expanded
        spec = manifest.get("spec", {})
        self.context = manifest.get("context", CommentedMap())
        if localEnv:
            self.context = localEnv.getContext(self.context)
        spec["inputs"] = self.context.get("inputs", spec.get("inputs", {}))
        # instantiate the tosca template, adding any external repositories we'll need
        self._setSpec(manifest)
        assert self.tosca

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

    def hasUri(self, uri):
        return uri in self.uris

    @property
    def metadata(self):
        return self.manifest.config.setdefault("metadata", CommentedMap())

    @property
    def yaml(self):
        return self.manifest.yaml

    def getBaseDir(self):
        return self.manifest.getBaseDir()

    def isPathToSelf(self, path):
        if self.path is None or path is None:
            return False
        if isinstance(path, Artifact):
            path = path.getPath()
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
    repositories = Manifest._getRepositories(config)
    repositories.pop("self", None)
    clone.manifest.path = destPath
    return clone


class YamlManifest(ReadOnlyManifest):
    def __init__(
        self, manifest=None, path=None, validate=True, localEnv=None, vault=None
    ):
        super(YamlManifest, self).__init__(manifest, path, validate, localEnv, vault)
        manifest = self.manifest.expanded
        spec = manifest.get("spec", {})
        status = manifest.get("status", {})

        self.changeLogPath = manifest.get("changeLog")
        self.jobsFolder = manifest.get("jobsFolder", "jobs")
        if not self.changeLogPath and localEnv:
            # save changes to a separate file if we're in a local environment
            self.changeLogPath = DefaultNames.JobsLog

        self.lastJob = manifest.get("lastJob")

        self.imports = Imports()
        self._importedManifests = {}

        if localEnv:
            for name in ["locals", "secrets"]:
                self.imports[name.rstrip("s")] = localEnv.getLocalInstance(
                    name, self.context
                )

        rootResource = self.createTopologyInstance(status)

        # create an new instances declared in the spec:
        for name, instance in spec.get("instances", {}).items():
            if not rootResource.findResource(name):
                # XXX like Plan.createResource() parent should be hostedOn target if defined
                self.createNodeInstance(name, instance or {}, rootResource)

        self._configureRoot(rootResource)
        self._ready(rootResource)

    def _configureRoot(self, rootResource):
        rootResource.imports = self.imports
        if (
            self.manifest.vault and self.manifest.vault.secrets
        ):  # setBaseDir() may create a new templar
            rootResource._templar._loader.set_vault_secrets(self.manifest.vault.secrets)
        rootResource.envRules = self.context.get("environment") or CommentedMap()

    def createTopologyInstance(self, status):
        """
        If an instance of the toplogy is recorded in status, load it,
        otherwise create a new resource using the the topology as its template
        """
        # XXX use the substitution_mapping (3.8.12) represent the resource
        template = self.tosca.topology
        operational = self.loadStatus(status)
        root = TopologyInstance(template, operational)
        root.setBaseDir(self.getBaseDir())

        # We need to set the environment as early as possible but not too early
        # and only once.
        # Now that we loaded the main manifest and set the root's baseDir
        # let's do it before we import any other manifests.
        # But only if we're the main manifest.
        if not self.localEnv or self.isPathToSelf(self.localEnv.manifestPath):
            if self.context.get("environment"):
                env = filterEnv(mapValue(self.context["environment"], root))
                intersectDict(os.environ, env)  # remove keys not in env
                os.environ.update(env)
            paths = self.localEnv and self.localEnv.getPaths()
            if paths:
                os.environ["PATH"] = (
                    os.pathsep.join(paths) + os.pathsep + os.environ.get("PATH", [])
                )
                logger.debug("PATH set to %s", os.environ["PATH"])

        importsSpec = self.context.get("external", {})
        # note: external "localhost" is defined in UNFURL_HOME's context by convention
        self.loadImports(importsSpec)
        self.loadConnections(self.context.get("connections"))

        # need to set rootResource before createNodeInstance() is called
        self.rootResource = root
        for key, val in status.get("instances", {}).items():
            self.createNodeInstance(key, val, root)
        return root

    def loadConnections(self, connections):
        if connections:
            # handle items like newname : oldname to rename merged connections
            renames = {
                (v if isinstance(v, six.string_types) else n): n
                for n, v in connections.items()
            }
            tpl = {}
            for name, c in connections.items():
                if isinstance(c, dict):
                    if "default_for" not in c:
                        c["default_for"] = "ANY"
                    tpl[renames[name]] = c
            tosca = ToscaSpec(
                dict(
                    tosca_definitions_version=TOSCA_VERSION,
                    topology_template=dict(
                        node_templates={}, relationship_templates=tpl
                    ),
                )
            )
            self.tosca.importConnections(tosca)

    def loadImports(self, importsSpec):
        """
        :manifest: artifact template (file and optional repository name)
        :instance: "*" or name # default is root
        :schema: # expected schema for attributes
        """
        for name, value in importsSpec.items():
            # load the manifest for the imported resource
            location = value.get("manifest")
            if not location:
                raise UnfurlError("Can not import '%s': no manifest specified" % (name))

            if "project" in location:
                importedManifest = self.localEnv.getExternalManifest(location)
                if not importedManifest:
                    raise UnfurlError(
                        "Can not import '%s': can't find project '%s'"
                        % (name, location["project"])
                    )
            else:
                # ensemble is in the same project
                baseDir = getattr(location, "baseDir", self.getBaseDir())
                artifact = Artifact(location, path=baseDir, spec=self.tosca)
                path = artifact.getPath()
                if self.isPathToSelf(path):
                    # don't import self (might happen when context is shared)
                    continue
                localEnv = self.localEnv or LocalEnv(path)
                importedManifest = localEnv.getManifest(path)

            uri = value.get("uri")
            if uri and not importedManifest.hasUri(uri):
                raise UnfurlError(
                    "Error importing '%s', uri mismatch for '%s'" % (path, uri)
                )
            rname = value.get("instance", "root")
            if rname == "*":
                rname = "root"
            # use findInstanceOrExternal() not findResource() to handle export instances transitively
            # e.g. to allow us to layer localhost manifests
            root = importedManifest.getRootResource()
            resource = root.findInstanceOrExternal(rname)
            if not resource:
                raise UnfurlError(
                    "Can not import '%s': instance '%s' not found" % (name, rname)
                )
            self.imports[name] = (resource, value)
            self._importedManifests[id(root)] = importedManifest

    def saveEntityInstance(self, resource):
        status = CommentedMap()
        status["template"] = resource.template.getUri()

        # only save the attributes that were set by the instance, not spec properties or attribute defaults
        # particularly, because these will get loaded in later runs and mask any spec properties with the same name
        if resource._attributes:
            status["attributes"] = resource._attributes
        if resource.shadow:
            # name will be the same as the import name
            status["imported"] = resource.name
        saveStatus(resource, status)
        if resource.created is not None:
            status["created"] = resource.created

        return (resource.name, status)

    def saveRequirement(self, resource):
        if not resource.lastChange and resource.status == Status.ok:
            # no reason to serialize requirements that haven't been instantiated
            return None
        name, status = self.saveEntityInstance(resource)
        status["capability"] = resource.parent.key
        return (name, status)

    def saveCapability(self, resource):
        if not resource.lastChange and resource.status == Status.ok:
            # no reason to serialize capabilities that haven't been instantiated
            return None
        return self.saveEntityInstance(resource)

    def saveResource(self, resource, discovered):
        name, status = self.saveEntityInstance(resource)
        if self.tosca.discovered and resource.template.name in self.tosca.discovered:
            discovered[resource.template.name] = self.tosca.discovered[
                resource.template.name
            ]

        if resource._capabilities:
            capabilities = list(
                filter(None, map(self.saveCapability, resource.capabilities))
            )
            if capabilities:
                status["capabilities"] = CommentedMap(capabilities)

        if resource._requirements:
            requirements = list(
                filter(None, map(self.saveRequirement, resource.requirements))
            )
            if requirements:
                status["requirements"] = CommentedMap(requirements)

        if resource.instances:
            status["instances"] = CommentedMap(
                map(lambda r: self.saveResource(r, discovered), resource.instances)
            )

        return (name, status)

    def saveRootResource(self, discovered):
        resource = self.rootResource
        status = CommentedMap()

        # record the input and output values
        status["inputs"] = serializeValue(resource.inputs.attributes)
        status["outputs"] = serializeValue(resource.outputs.attributes)

        saveStatus(resource, status)
        # getOperationalDependencies() skips inputs and outputs
        status["instances"] = CommentedMap(
            map(
                lambda r: self.saveResource(r, discovered),
                resource.getOperationalDependencies(),
            )
        )
        return status

    def saveJobRecord(self, job):
        """
        .. code-block:: YAML

          jobId: 1
          startCommit: # commit when job began
          startTime:
          workflow:
          options: # job options set by the user
          summary:
          specDigest:
          lastChangeId: # the changeid of the job's last task
          endCommit:   # commit updating status (only appears in changelog file)
        """
        output = CommentedMap()
        output["changeId"] = job.changeId
        output["startTime"] = job.getStartTime()
        if job.previousId:
            output["previousId"] = job.previousId
        options = job.jobOptions.getUserSettings()
        output["workflow"] = options.pop("workflow", Defaults.workflow)
        output["options"] = options
        output["summary"] = job.stats(asMessage=True)
        if self.currentCommitId:
            output["startCommit"] = self.currentCommitId
        output["specDigest"] = self.specDigest
        return saveStatus(job, output)

    def saveJob(self, job):
        discovered = CommentedMap()
        changed = self.saveRootResource(discovered)

        # update changed with includes, this may change objects with references to these objects
        self.manifest.restoreIncludes(changed)
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
            patchDict(self.manifest.config["lock"], lock, cls=CommentedMap)

        # modify original to preserve structure and comments
        if "status" not in self.manifest.config:
            self.manifest.config["status"] = {}
        if not self.manifest.config["status"]:
            self.manifest.config["status"] = changed
        else:
            patchDict(self.manifest.config["status"], changed, cls=CommentedMap)

        jobRecord = self.saveJobRecord(job)
        if job.workDone:
            self.manifest.config["lastJob"] = jobRecord
            changes = map(saveTask, job.workDone.values())
            if self.changeLogPath:
                self.manifest.config["changeLog"] = self.changeLogPath
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

    def commitJob(self, job):
        if job.planOnly:
            return
        if job.dryRun:
            logger.info("printing results from dry run")
            if not job.out and self.manifest.path:
                job.out = sys.stdout
        jobRecord, changes = self.saveJob(job)
        if not changes:
            logger.info("job run didn't make any changes; nothing to commit")
            return
        if job.dryRun:
            return

        if self.changeLogPath:
            jobLogPath = self.saveChangeLog(jobRecord, changes)
            self.appendLog(job, jobRecord, jobLogPath)

        if job.commit and self.repo:
            if job.message is not None:
                message = job.message
            else:
                message = self.getDefaultCommitMessage()
            self.commit(message, True)

    def getDefaultCommitMessage(self):
        jobRecord = self.manifest.config.get("lastJob")
        if jobRecord:
            return "Updating status for job %s" % jobRecord["changeId"]
        else:
            return "Commit by Unfurl"

    def getRepoStatuses(self, dirty=False):
        return [
            'Status for "%s" at %s:\n%s\n\n' % (r.name, r.workingDir, r.status())
            for r in self.repositories.values()
            if r.repo and (not dirty or r.isDirty())
        ]

    def addAll(self):
        for repository in self.repositories.values():
            if not repository.readOnly and repository.isDirty():
                repository.addAll()

    def commit(self, msg, addAll):
        committed = 0
        for repository in self.repositories.values():
            if repository.repo == self.repo:
                continue
            if not repository.readOnly and repository.isDirty():
                retVal = repository.commit(msg, addAll)
                committed += 1
                logger.info(
                    "committed %s to %s: %s", retVal, repository.workingDir, msg
                )
        # if manifest was changed: # e.g. calling commit after a job was run
        #    if commits were made writeLock and save updated manifest??
        #    (note: endCommit will be omitted as changes.yaml isn't updated)
        ensembleRepo = self.repositories["self"]
        if ensembleRepo.isDirty():
            retVal = ensembleRepo.commit(msg, addAll)
            committed += 1
            logger.info("committed %s to %s: %s", retVal, ensembleRepo.workingDir, msg)

        return committed

    def getChangeLogPath(self):
        return os.path.join(self.getBaseDir(), self.changeLogPath)

    def getJobLogPath(self, startTime, ext=".yaml"):
        name = os.path.basename(self.getChangeLogPath())
        # try to figure out any custom name pattern from changelogPath:
        defaultName = os.path.splitext(DefaultNames.JobsLog)[0]
        currentName = os.path.splitext(name)[0]
        prefix, _, suffix = currentName.partition(defaultName)
        fileName = prefix + "job" + startTime + suffix + ext
        return os.path.join(self.jobsFolder, fileName)

    def appendLog(self, job, jobRecord, jobLogPath):
        logPath = self.getChangeLogPath()
        jobLogRelPath = os.path.relpath(jobLogPath, os.path.dirname(logPath))
        if not os.path.isdir(os.path.dirname(logPath)):
            os.makedirs(os.path.dirname(logPath))
        logger.info("saving changelog to %s", logPath)
        tasks = job.workDone.values()
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

            for task in tasks:
                attrs = dict(
                    status=task.status.name,
                    target=task.target.key,
                    summary=task.summary(),
                )
                f.write(task.log(attrs))

    def saveChangeLog(self, jobRecord, newChanges):
        try:
            changelog = CommentedMap()
            fullPath = self.getJobLogPath(jobRecord["startTime"])
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
            raise UnfurlError("Error saving changelog %s" % self.changeLogPath, True)
