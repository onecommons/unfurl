=========================
Repositories and Packages
=========================

Unfurl can track the upstream repositories that contain the TOSCA templates, artifacts, and code used during deployment. It will download local copies, detect upstream changes, and apply semantic versioning to detecting conflicts and potentially breaking changes.

In Unfurl, a ``repository`` is a TOSCA abstraction that can represent any collection of artifacts. For example, a repository can be git repository, an OCI (Docker) container registry, or a local file directory. You can also define virtual repositories with an explicit list of artifacts.

A ``package`` is a similar abstraction in that a package is a collection of artifacts but unlike a repository it isn't tied to a particular location -- instead it has a `package identifier <package identifiers>` that uniquely identifies the package. In addition, a package also can have a revision identifier associated with it that (optionally) conforms to the semantic versioning specification.

In a manner similar to common package managers like node.js' npm or go modules, Unfurl will apply package resolution to make sure all references to a package use the same revision -- for example, when multiple repository definitions refer to the same package.

.. _repository:

Repositories
============

You declare repositories using TOSCA's `repository <tosca_repositories>` syntax at the top-level of a TOSCA service template or in the `environment` section of your project's `unfurl.yaml`.

You can also create "virtual" repositories by declaring node templates of type ``unfurl.nodes.Repository`` or ``unfurl.nodes.LocalRepository``. This will instantiate a repository of with same name as the template and it will contain the artifacts listed in the ``artifacts`` section of the node template -- here's `an example <https://github.com/onecommons/unfurl/blob/f5da8de13ae2dcce293508c4ccac9b373e66dd49/unfurl/tosca_plugins/artifacts.yaml#L140>`_.

The contents of a `repository <tosca_repositories>` can be referenced in the following ways:

* In TOSCA `imports` statements.
* In TOSCA :tosca_spec:`artifacts <_Toc50125252>` definitions.
* In the `+include YAML merge directive <YAML Merge directives>`.
* In runtime expressions such as :std:ref:`abspath` and :std:ref:`get_dir`.

File paths in configuration files (e.g. include directives, imports, and external file references) are relative to the project and Unfurl will raise error if a file path would escapes outside the project.
To access file outside a project, declare a `repository<repositories>` with an absolute file URL and use it in the file reference.


Predefined Repositories
-----------------------

There are three predefined repositories:

"self", which represents the location the ensemble lives in -- it will be a "git-local:" URL if no git remote has been set, or a "file:" URL if the ensemble is not part of a git repository at all.

"spec" which, unless explicitly declared, defaults to the project root or the ensemble itself if it is not part of a project.

"unfurl" which points to the Python package of the unfurl process -- this can be used to load configurators and templates
that ship with Unfurl.

* Allow service templates to declare the unfurl version they are compatible with.

They can do this be declaring a repository for the unfurl package like so:

.. code-block:: yaml

        repositories:
          unfurl:
            url: https://github.com/onecommons/unfurl
            revision: v0.9.1

Unfurl will still resolve imports in the unfurl package using the local installed version of unfurl but it will raise an error if it isn't compatible with the version declared here.

Packages
========

A repository will also be treated as a ``package`` if the repository URL points to a git repository and can be transformed to match the package identifier syntax described below.

A repository that is a package can also have a revision associated with it, either embedded in the URL (git URLs have can have refs in the URL fragment) or by adding a ``revision`` key to the `repository definition <tosca_repositories>`.

If a revision matches the `semantic versioning syntax <https://semver.org/>`_ it will be treated as a semantic version, otherwise it is treated as a opaque identifier with no implied semantics. For example, a revision can be  a git branch whose contents changes over time.

If no revision is specified Unfurl will attempt to detect the latest version of the package looking on the remote repository a git tag that looks like the most recent semantic version (see https://go.dev/ref/mod#vcs-version for the algorithm) and set the local repository contents to that tag. If none is found, the latest commit from repository's default branch will be used. To override version detection, set the revision to a branch like "main".

Package Resolution
====================

If a repository is treated as a package, multiple repositories that resolve to the same package will use the same local content, even the repository definitions use different URLs (this can happen with `package rules`). So if repository specify different revisions those revision need to be compatible.

If the revision looks like a semantic version the `semantic versioning rules <https://semver.org/>`_ for version compatibility will be used otherwise the version specifiers need to be identical. If the revisions are not compatible, Unfurl will abort with an error. Otherwise, like Go, it will choose the minimum required version for the repositories.

Package identifiers
===================

Here are some examples of package ids:

  ``unfurl.cloud/onecommons/unfurl-types``

  ``example.org``

  ``example.org/mypackage/v2``

If the package references to a path in a git repository we follow Go's convention for including the path after ".git/" in the name. For example:

  ``onecommons.org/unfurl-type.git/anotherpackage/v2``

  ``gitlab.com/onecommons/unfurl-types.git/v2``

Package identifiers resolve to a git repository following the algorthims for `Go modules <https://go.dev/ref/mod>`_ Repository declarations can include required version either by including a ``revision`` field or by including it as a URL fragment in the package identifier (e.g ``#v1.1.0``).

Locked ensembles
================

An ensemble's manifest may contain a `lock section <lock>` that records the exact version and state of the repositories, packages, and deployment tools used when the ensemble was last deployed. It is conceptually similar to the lock files used in development environments for building and packaging applications (such as node.js' yarn.lock and package-lock.json or Rust's cargo.lock) .

Once an ensemble is deployed and is live, if a repository appears in the `lock section <lock>` of the ensemble, the revision recorded in the `lock section <lock>` for the repository will be used in subsequent jobs for that ensemble, overriding other package resolution logic.

If Unfurl was unable to find any semantic version tags for a repository, the `lock section <lock>` will record this. In that case, subsequent deployments will attempt to fetch the earliest semantic version tag if found and no other revision was specified (as opposed to the default behavior of fetching the latest version tag). This is to handle the case when a package transitions from being unreleased to released.

Package Rules
=============

You can define package rules that are applied to package definitions, overriding the location or version of a package or replacing the package identifier.

If a key in a `repositories` section look like package identifier that it will be treated as a package rule instead of a repository definition. Some examples:

.. code:: yaml

    environments:
      defaults:
        repositories:
          # set the repository URL and optionally the version for the given package
          unfurl.cloud/onecommons/blueprints/wordpress:
            url: https://unfurl.cloud/user/repo.git#main # set the package to a specific repository url that also sets the branch

          # if url is set to a package identifier, replace a package with another
          unfurl.cloud/onecommons/unfurl-types:
            url: github.com/user1/myfork

          # A trailing * applies the rule to all packages that match
          unfurl.cloud/onecommons/*:
            url: https://staging.unfurl.cloud/onecommons/*

          # replace for a particular package, version combination
          unfurl.cloud/onecommons/blueprints/ghost#v1.6.0:
            url: github.com/user1/myforks.git/ghost
            revision: 1.6.1 # e.g. a security patch


You can also set these rules with the ``UNFURL_PACKAGE_RULES`` environment variable where the repository key and the ``url`` value are paired together and separated by spaces. If you want to specify the ``revision``, spell it as an URL fragment ("#revision") and append that to the URL if you need to specify both. This example defines two rules:

```UNFURL_PACKAGE_RULES="unfurl.cloud/onecommons/* #main unfurl.cloud/onecommons/unfurl-types github.com/user1/myfork"```

The first rule sets the revision of matching packages to the branch "main", and the second replaces one package with another package.

