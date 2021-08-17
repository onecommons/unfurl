# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
from ruamel.yaml.comments import CommentedMap
from . import __version__
from .util import get_package_digest


class Lock:
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
        lock["runtime"] = self.lock_runtime()
        lock["repositories"] = [
            repo.lock() for repo in self.ensemble.repositories.values()
        ]
        ensembles = self.lock_ensembles()
        if ensembles:
            lock["ensembles"] = ensembles

        # XXX artifacts should be saved as part of status because different tasks
        # could use different versions of the same artifact at different times
        # artifacts = []
        # for task in tasks:
        #     self.lock_task(task, artifacts)
        # if artifacts:
        #     lock["artifacts"] = artifacts
        return lock

    def lock_runtime(self):
        ensemble = self.ensemble
        record = CommentedMap()
        record["unfurl"] = CommentedMap(
            (("version", __version__(True)), ("digest", get_package_digest()))
        )
        if ensemble.localEnv and ensemble.localEnv.toolVersions:
            record["toolVersions"] = {
                n: list(v) for n, v in ensemble.localEnv.toolVersions.items()
            }
        # XXX Pipfile.lock: _meta.hash, python version
        return record

    def lock_ensembles(self):
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

    def lock_task(self, task, artifacts):
        # XXX unused
        instance = task.target
        artifactName = "image"
        artifact = instance.artifacts.get(artifactName)
        if artifact:
            docker_container = task.outputs and task.outputs.get("docker_container")
            if docker_container and "Image" in docker_container:
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
