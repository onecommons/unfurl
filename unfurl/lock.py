# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
from typing import TYPE_CHECKING, Dict
from ruamel.yaml.comments import CommentedMap

from . import __version__
from .packages import Package, PackagesType, get_package_id_from_url
from .util import get_package_digest
from .logs import getLogger

logger = getLogger("unfurl")

if TYPE_CHECKING:
    from .manifest import Manifest
    from .yamlmanifest import YamlManifest


class Lock:
    def __init__(self, ensemble: "YamlManifest"):
        self.ensemble = ensemble

    @staticmethod
    def apply_to_packages(locked: dict, manifest: "Manifest"):
        for repo_dict in locked["repositories"]:
            if not repo_dict.get("name"):
                continue
            repository = manifest.repositories.get(repo_dict["name"])
            if repository and repository.package is False:
                continue
            package_id, url, revision = get_package_id_from_url(repo_dict["url"])
            if package_id:
                commit = repo_dict.get("commit")
                revision = repo_dict.get("tag")
                if not commit:  # old lock format
                    commit = repo_dict.get("revision")
                    revision = None
                existing_package = manifest.packages.get(package_id)
                if existing_package:
                    if existing_package.lock_to_commit:
                        continue
                    if revision and existing_package.revision == revision:
                        continue
                    if not Package(package_id, url, revision).is_compatible_with(existing_package):
                        logger.warning(
                            "locking packages to a incompatible revision % for %s",
                            revision,
                            existing_package,
                        )
                    existing_package.revision = revision
                    package = existing_package
                else:
                    package = Package(package_id, url, revision)
                    manifest.packages[package_id] = package
                if not revision:
                    package.lock_to_commit = True
                logger.verbose(
                    "setting package %s (%s) to revision %s from lock section",
                    package_id,
                    repo_dict.get("name"),
                    revision,
                )

    # XXX
    # def validate_runtime(self):
    #   make sure the current environment is compatible with this lock:
    #   compare unfurl version and repositories (as packages)

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
               revision:  # intended revision (branch or tag) declared by user
               commit:    # current commit
               branch:    # current commit is on this branch
               tag:       # current commit is on this tag
               initial:   # initial commit
               origin:    # origin url
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
            # skip imports that were added while creating shadow instances
            if ":" in name or not _import.spec:
                continue
            root = _import.external_instance.root
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
            docker_container = task.outputs and task.outputs.get("container")
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
