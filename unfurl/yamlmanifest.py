"""Loads and saves a Unfurl manifest with the following format
"""
from __future__ import absolute_import
import six
import sys
import collections
import numbers
import os.path
import itertools

from .util import UnfurlError, toYamlText
from .merge import restoreIncludes, patchDict
from .yamlloader import YamlConfig, yaml
from .result import serializeValue
from .support import ResourceChanges, Defaults, Imports
from .localenv import LocalEnv
from .job import JobOptions, Runner
from .manifest import Manifest
from .tosca import Artifact
from .runtime import TopologyInstance

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
        d[k] = v[ResourceChanges.attributesIndex] or {}
        if v[ResourceChanges.statusIndex] is not None:
            d[k][".status"] = v[ResourceChanges.statusIndex].name
        if v[ResourceChanges.addedIndex]:
            d[k][".added"] = v[ResourceChanges.addedIndex]
    return d


def saveStatus(operational, status=None):
    if status is None:
        status = CommentedMap()
    if not operational.lastChange and not operational.status:
        # skip status
        return status

    readyState = CommentedMap([("effective", operational.status.name)])
    if operational.localStatus is not None:
        readyState["local"] = operational.localStatus.name
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
        return CommentedMap((key, saveResult(v)) for key, v in value.items())
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
  parentId:
  target:
  implementation:
  inputs:
  changes:
  dependencies:
  messages:
  result:  # an object or "skipped"
  """
    output = CommentedMap()
    output["changeId"] = task.changeId
    output["parentId"] = task.parentId
    if task.target:
        output["target"] = task.target.key
    saveStatus(task, output)
    output["implementation"] = saveConfigSpec(task.configSpec)
    if task._inputs:  # only serialize if inputs were already successfully instantiated
        output["inputs"] = serializeValue(task.inputs)
    changes = saveResourceChanges(task._resourceChanges)
    if changes:
        output["changes"] = changes
    if task.messages:
        output["messages"] = task.messages
    dependencies = [saveDependency(val) for val in task.dependencies.values()]
    if dependencies:
        output["dependencies"] = dependencies
    if task.result:
        result = task.result.result
        if result:
            output["result"] = saveResult(result)
    else:
        output["result"] = "skipped"

    return output


class YamlManifest(Manifest):
    def __init__(self, manifest=None, path=None, validate=True, localEnv=None):
        assert not (localEnv and (manifest or path))  # invalid combination of args
        # localEnv and repo are needed by loadHook before base class initialized
        self.localEnv = localEnv
        self.repo = localEnv and localEnv.instanceRepo
        self.manifest = YamlConfig(
            manifest,
            path or localEnv and localEnv.manifestPath,
            validate,
            os.path.join(_basepath, "manifest-schema.json"),
            self.loadYamlInclude,
        )
        manifest = self.manifest.expanded
        spec = manifest.get("spec", {})
        super(YamlManifest, self).__init__(spec, self.manifest.path, localEnv)
        assert self.tosca
        status = manifest.get("status", {})

        self.changeLogPath = manifest.get("changeLog")
        if self.changeLogPath:
            fullPath = os.path.join(self.getBaseDir(), self.changeLogPath)
            if os.path.exists(fullPath):
                changelog = YamlConfig(
                    None,
                    fullPath,
                    validate,
                    os.path.join(_basepath, "changelog-schema.json"),
                )
                changes = changelog.config.get("changes", [])
            else:
                if status:
                    logger.warning("missing changelog: %s", fullPath)
                changes = manifest.get("changes", [])
        else:
            changes = manifest.get("changes", [])
            if localEnv:
                # save changes to a separate file if we're in a local environment
                self.changeLogPath = "changes.yaml"

        self.changeSets = dict(
            (c.get("changeId", c.get("jobId", 0)), c) for c in changes
        )
        lastChangeId = self.changeSets and max(self.changeSets.keys()) or 0

        importsSpec = manifest.get("imports", {})
        if localEnv:
            # make sure these exist, localEnv will always add them
            importsSpec.setdefault("local", {})
            importsSpec.setdefault("secret", {})
            importsSpec.setdefault("localhost", {})
        self.imports = self.loadImports(importsSpec)

        rootResource = self.createTopologyInstance(status)
        # create an new instances declared in the spec:
        for name, instance in spec.get("instances", {}).items():
            if not rootResource.findResource(name):
                # XXX like Plan.createResource() parent should be hostedOn target if defined
                self.createNodeInstance(name, instance or {}, rootResource)

        rootResource.imports = self.imports
        rootResource.setBaseDir(self.getBaseDir())

        self._ready(rootResource, lastChangeId)

    def getBaseDir(self):
        return self.manifest.getBaseDir()

    def createTopologyInstance(self, status):
        """
    If an instance of the toplogy is recorded in status, load it,
    otherwise create a new resource using the the topology as its template
    """
        # XXX use the substitution_mapping (3.8.12) represent the resource
        template = self.tosca.topology
        operational = self.loadStatus(status)
        root = TopologyInstance(template, operational)
        # need to set it before createNodeInstance() is called
        self.rootResource = root
        for key, val in status.get("instances", {}).items():
            self.createNodeInstance(key, val, root)
        return root

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
        if resource.createdOn:  # will be a ChangeRecord
            status["createdOn"] = resource.createdOn.changeI

        return (resource.name, status)

    def saveRequirement(self, resource):
        name, status = self.saveEntityInstance(resource)
        status["capability"] = resource.parent.key
        return (name, status)

    def saveResource(self, resource, workDone):
        name, status = self.saveEntityInstance(resource)
        if resource._capabilities:
            status["capabilities"] = CommentedMap(
                map(self.saveEntityInstance, resource.capabilities)
            )

        if resource._requirements:
            status["requirements"] = CommentedMap(
                map(self.saveRequirement, resource.requirements)
            )

        if resource.instances:
            status["instances"] = CommentedMap(
                map(lambda r: self.saveResource(r, workDone), resource.instances)
            )

        return (name, status)

    def saveRootResource(self, workDone):
        resource = self.rootResource
        status = CommentedMap()

        # record the input and output values
        # XXX make sure sensative values are redacted (and need to check for 'sensitive' metadata)
        status["inputs"] = serializeValue(resource.inputs.attributes)
        status["outputs"] = serializeValue(resource.outputs.attributes)

        saveStatus(resource, status)
        # getOperationalDependencies() skips inputs and outputs
        status["instances"] = CommentedMap(
            map(
                lambda r: self.saveResource(r, workDone),
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
        output["jobId"] = job.changeId
        output["startTime"] = str(job.startTime)
        options = job.jobOptions.getUserSettings()
        output["workflow"] = options.pop("workflow", Defaults.workflow)
        output["options"] = options
        output["summary"] = job.stats(asMessage=True)
        if self.currentCommitId:
            output["startCommit"] = self.currentCommitId
        output["specDigest"] = self.specDigest
        output["lastChangeId"] = job.runner.lastChangeId
        return saveStatus(job, output)

    def saveJob(self, job):
        changed = self.saveRootResource(job.workDone)
        # XXX imported resources need to include its repo's workingdir commitid in their status
        # status and job's changeset also need to save status of repositories
        # that were accessed by loadFromRepo() and add them with commitid and repotype
        # note: initialcommit:requiredcommit means any repo that has at least requiredcommit

        # update changed with includes, this may change objects with references to these objects
        restoreIncludes(
            self.manifest.includes, self.manifest.config, changed, cls=CommentedMap
        )
        # modify original to preserve structure and comments
        if "status" not in self.manifest.config:
            self.manifest.config["status"] = {}

        if not self.manifest.config["status"]:
            self.manifest.config["status"] = changed
        else:
            patchDict(self.manifest.config["status"], changed, cls=CommentedMap)

        jobRecord = self.saveJobRecord(job)
        if job.workDone:
            self.manifest.config["latestChange"] = jobRecord
            changes = map(saveTask, job.workDone.values())
            if self.changeLogPath:
                self.manifest.config["changeLog"] = self.changeLogPath
            else:
                self.manifest.config.setdefault("changes", []).extend(changes)
        else:
            # no work was done, so bother recording this job
            changes = []

        if job.out:
            self.dump(job.out)
        else:
            output = six.StringIO()
            self.dump(output)
            job.out = output
            if self.manifest.path:
                with open(self.manifest.path, "w") as f:
                    f.write(output.getvalue())
        return jobRecord, changes

    def dump(self, out=sys.stdout):
        try:
            self.manifest.dump(out)
        except:
            raise UnfurlError("Error saving manifest %s" % self.manifest.path, True)

    def hasWritableRepo(self):
        if self.repo:
            repos = self._getRepositories(self.manifest.expanded)
            repoSpec = repos.get("instance", repos.get("spec"))
            if repoSpec:
                initialCommit = repoSpec and repoSpec.get("metadata", {}).get(
                    "initial-commit"
                )
                return initialCommit == self.repo.getInitialRevision()
        return False

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

        doCommit = job.commit and self.hasWritableRepo()
        if doCommit:
            self.repo.commitFiles(
                [self.manifest.path], "Updating status for job %s" % job.changeId
            )
            jobRecord["endCommit"] = self.repo.revision
        if self.changeLogPath:
            self.saveChangeLog(jobRecord, changes)
            if doCommit:
                self.repo.commitFiles(
                    [os.path.join(self.getBaseDir(), self.changeLogPath)],
                    "Updating changelog for job %s" % job.changeId,
                )
        if doCommit:
            logger.info("committed instance repo changes: %s", self.repo.revision)
        elif job.commit and self.repo:
            logger.info(
                "couldn't commit, the current repository with initial revision %s was not specified",
                self.repo.getInitialRevision(),
            )

    def saveChangeLog(self, jobRecord, newChanges):
        """
    manifest: manifest.yaml
    changes:
    """
        try:
            changelog = CommentedMap()
            changelog["manifest"] = os.path.basename(self.manifest.path)
            # put jobs before their child tasks
            key = lambda r: r.get("lastChangeId", r.get("changeId", 0))
            changes = itertools.chain([jobRecord], newChanges, self.changeSets.values())
            changelog["changes"] = sorted(changes, key=key, reverse=True)
            output = six.StringIO()
            yaml.dump(changelog, output)
            fullPath = os.path.join(self.getBaseDir(), self.changeLogPath)
            logger.info("saving changelog to %s", fullPath)
            with open(fullPath, "w") as f:
                f.write(output.getvalue())
        except:
            raise UnfurlError("Error saving changelog %s" % self.changeLogPath, True)

    def loadImports(self, importsSpec):
        """
      file: local/path # for now
      repository: uri or repository name in TOSCA template
      commitId:
      resource: name # default is root
      attributes: # queries into resource
      properties: # expected schema for attributes
    """
        imports = Imports()
        imported = {}
        for name, value in importsSpec.items():
            resource = self.localEnv and self.localEnv.getLocalResource(name, value)
            if not resource:
                # load the manifest for the imported resource
                file = value.get("file")
                if not file:
                    raise UnfurlError("Can not import '%s': no file specified" % (name))
                location = dict(file=file, repository=value.get("repository"))
                key = tuple(location.values())
                importedManifest = imported.get(key)
                if not importedManifest:
                    # if location resolves to an url to a git repo
                    # loadFromRepo will find or create a working dir
                    path, yamlDict = self.loadFromRepo(
                        Artifact(location), self.getBaseDir()
                    )
                    importedManifest = YamlManifest(yamlDict, path=path)
                    imported[key] = importedManifest

                rname = value.get("instance", "root")
                if rname == "*":
                    rname = "root"
                resource = importedManifest.getRootResource().findResource(rname)
                if "inheritHack" in value:  # set by getLocalResource() above
                    value["inheritHack"]._attributes["inheritFrom"] = resource
                    resource = value.pop("inheritHack")

            if not resource:
                raise UnfurlError(
                    "Can not import '%s': resource '%s' not found" % (name, rname)
                )
            imports[name] = (resource, value)
        return imports


def runJob(manifestPath=None, _opts=None):
    _opts = _opts or {}
    localEnv = LocalEnv(manifestPath, _opts.get("home"))
    opts = JobOptions(**_opts)
    path = localEnv.manifestPath
    if opts.planOnly:
        logger.info("creating %s plan for %s", opts.workflow, path)
    else:
        logger.info("running %s job for %s", opts.workflow, path)

    logger.info("loading manifest at %s", path)
    try:
        manifest = YamlManifest(localEnv=localEnv)
    except Exception as e:
        logger.error(
            "failed to load manifest at %s: %s",
            path,
            str(e),
            exc_info=opts.verbose >= 2,
        )
        return None

    runner = Runner(manifest)
    return runner.run(opts)
