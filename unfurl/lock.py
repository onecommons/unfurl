# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
from typing import TYPE_CHECKING, Dict
from ruamel.yaml.comments import CommentedMap

from .repo import RepoView

from . import __version__
from .packages import Package, PackageSpec, PackagesType, get_package_id_from_url
from .util import get_package_digest
from .logs import getLogger

logger = getLogger("unfurl")

if TYPE_CHECKING:
    from .manifest import Manifest
    from .yamlmanifest import YamlManifest


class Lock:
    def __init__(self, ensemble: "YamlManifest"):
        self.ensemble = ensemble

    def find_packages(self):
        lock = self.ensemble.manifest.config.get("lock")
        if lock:
            yield from self._find_packages(lock, self.ensemble)

    @staticmethod
    def _find_packages(locked: dict, manifest: "Manifest"):
        old_format = "package_rules" not in locked
        for repo_dict in locked.get("repositories") or []:
            package_id = repo_dict.get("package_id")
            if package_id:
                yield package_id, repo_dict
            elif old_format and repo_dict.get("name"):
                repository = manifest.repositories.get(repo_dict["name"])
                if repository and repository.package is False:
                    continue
                package_id, url, revision = get_package_id_from_url(repo_dict["url"])
                if package_id:
                    yield package_id, repo_dict

    @staticmethod
    def find_package(lock: dict, manifest: "Manifest", package):
        # we want to find the repository that was originally used for this package
        # each repository in the lock has one package
        # but multiple repositories can point at the same package (tracked in package.repositories)
        # look for the first match (they will all point to the same git repo)
        # But package rules can map many package_ids to one package
        # so we want to search for the original package id after applying the package rules saved with the lock
        if "package_rules" in lock:
            rules = [PackageSpec(*spec.split()) for spec in lock.get("package_rules", [])]
        else:
            rules = manifest.package_specs
        PackageSpec.update_package(rules, package)
        for package_id, repo_dict in Lock._find_packages(lock, manifest):
            if package_id == package.package_id:
                return repo_dict
        return None

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
          package_rules:
            - "package rule"
          repositories:
             - package_id:# set if repository has a package
               name:      # name repository (set if package_id is missing)
               url:       # repository url
               revision:  # intended revision (branch or tag) declared by user
               discovered_revision: # "", revision, or "(MISSING)"
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
        lock["package_rules"] = [
            spec.as_env_value()
            for spec in self.ensemble.package_specs if spec.package_spec != "github.com/onecommons/unfurl"
        ]

        repositories = self.lock_repositories()
        lock["repositories"] = repositories

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

    def lock_repositories(self):
        # create lock records for each git repo referenced by package or a Repository
        # (localenv might record more local git repos but don't include ones that weren't referenced)
        repositories = []
        repos = set()
        for package_id, package in self.ensemble.packages.items():
            if package and package.repositories:
                repo_view = package.repositories[0]
                if repo_view.repo and id(repo_view.repo) not in repos:
                    repos.add(id(repo_view.repo))
                    repositories.append(repo_view.lock())
        # find non-package repositories
        for repo_view in self.ensemble.repositories.values():
            if repo_view.repo and id(repo_view.repo) not in repos:
                repos.add(id(repo_view.repo))
                repositories.append(repo_view.lock())
        return repositories

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
