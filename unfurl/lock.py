# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
from ruamel.yaml.comments import CommentedMap
from . import __version__
from .util import getPackageDigest

class Lock(object):
    def __init__(self, ensemble):
        self.ensemble = ensemble

    def lock(self):
        """
        lock:
          runtime:
            unfurl:
              version
              digest
            toolVersions:
          repositories:
             - name:
               url:
               revision:
               initial:
               origin:
          ensembles:
            name: # e.g. localhost
              uri:
              changeId:
              digest:
              manifest:
        """
        lock = CommentedMap()
        lock["runtime"] = self.lockRuntime()
        lock["repositories"] = [
            repo.lock() for repo in self.ensemble.repositories.values()
        ]
        ensembles = self.lockEnsembles()
        if ensembles:
            lock["ensembles"] = ensembles

        # XXX artifacts should be saved as part of status because different tasks
        # could use different versions of the same artifact at different times
        # artifacts = []
        # for task in tasks:
        #     self.lockTask(task, artifacts)
        # if self.artifacts:
        #     lock["artifacts"] = self.artifacts
        return lock

    def lockRuntime(self):
        ensemble = self.ensemble
        record = CommentedMap()
        record["unfurl"] = CommentedMap(
            (("version", __version__(True)), ("digest", getPackageDigest()))
        )
        if ensemble.localEnv and ensemble.localEnv.toolVersions:
            record["toolVersions"] = {
                n: list(v) for n, v in ensemble.localEnv.toolVersions.items()
            }
        # XXX Pipfile.lock: _meta.hash, python version
        return record

    def lockEnsembles(self):
        ensemble = self.ensemble
        ensembles = CommentedMap()
        for name, _import in ensemble.imports.items():
            root = _import.resource.root.shadow or _import.resource.root
            manifest = ensemble._importedManifests.get(id(root))
            if not manifest:
                continue
            lastJob = manifest.lastJob or {}
            ensembles[name] = CommentedMap(
                [
                    ("uri", manifest.uri),
                    ("changeId", lastJob.get("changeId")),
                    ("digest", manifest.specDigest),
                    ("manifest", _import.spec["manifest"]),
                ]
            )
        return ensembles

    def lockTask(self, task, artifacts):
        instance = task.target
        artifactName = "image"
        artifact = instance.artifacts.get(artifactName)
        if artifact:
            docker_container = task.outputs.get("docker_container")
            if docker_container:
                spec = dict(
                    instance=instance.name,
                    artifact=artifactName,
                    digest=docker_container["Image"],
                    changeId=task.changeId,
                )
                path = task.outputs.get("image_path")
                if path:
                    spec["name"] = path
                artifacts.append(CommentedMap(spec.items()))
