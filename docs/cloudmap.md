# Cloud Maps

A cloud map is a YAML document that contains a catalog of git repositories and the software, services, and applications that can be built or deployed from them. A cloud map contains project metadata and other repository metadata such as mirrors and forks that are not stored in git repositories themselves.
What makes a cloud map more than just a simple catalog of repositories is that it uses TOSCA schemas to express the dependencies between artifacts and applications produced by the repositories.

Some of the ways Unfurl and Unfurl Cloud uses cloud maps:

* Unfurl can search a cloud map for blueprints to add to a project.
* Unfurl's UI server (and Unfurl Cloud) uses the cloud map to find compatible types and applications when building blueprints and deployments.
* Unfurl uses a cloud map to synchronize project metadata and mirrors of git repositories with git hosting services including Gitlab, Unfurl Cloud or just local file directories.
* Unfurl Cloud syncs its public [catalog](https://unfurl.cloud/blueprints) of popular open-source applications to our public [cloud map](https://github.com/onecommons/cloudmap.git) and hosts a [visualization](https://unfurl.cloud/cloud) of it.

## Key Concepts

A "repository host" is a service that hosts git repositories. These types of git hosting services are currently supported: "local", "github", "gitlab", and "unfurl.cloud". Local is just a local directory containing git repositories.

Cloud maps are stored in a separate git repository that Unfurl clones and manages. If one is not configured it defaults to <https://github.com/onecommons/cloudmap.git>.

Unfurl maintains branches in the cloud map repository that reflect the current state of each repository host. When synchronizing a repositories host with the cloud map merges  its branch with the main branch.

## How To...

### Clone from a cloud map

When using the {cli}`unfurl clone<unfurl-clone>` command, you can clone from the project's default cloud map by using an URL of the form ``cloudmap:<package_id>`` as the ``SOURCE`` argument.
Unfurl will then search the cloud map for a repository whose key matches ``<package_id>``.

### Update a cloud map

``unfurl cloudmap --import HOST`` will update the cloudmap with latest from the given repository host.  ``HOST`` can be an url, the name of the host (as declared in the {ref}`cloudmaps/hosts<configuration>` section of the project's default environment), or "local" to update from the local files system.

For example, this command will update the cloudmap with metadata from local clones of git repositories:

`unfurl cloudmap --import local --clone-root ../my-repos`

where `../my-repos` is the path to a directory containing one or more git repositories.

If repository host is remote, the import option will also pull the latest upstream changes into local git clones if the ``clone_root`` option is specified.

Import updates the cloudmap in a local branch, so you have manually merge those changed upstream if desired or use the `--sync` option described below.

### Update a repository host with a cloud map

You can update a gitlab or unfurl cloud instance with latest in the cloud map using the ``--export`` option of the {cli}`unfurl cloudmap<unfurl-cloudmap>` command.

For each matching repository in the cloudmap, a new project will be created if missing or existing project metadata will be updated. Then local git clones of the projects will be use to merge and push git changes to the host. The ``--force`` flag can be used to force push changes instead of merging.

For example: `unfurl cloudmap --export my_gitlab --namespace my_group`

### Syncing

The ``--sync`` option of the {cli}`unfurl cloudmap<unfurl-cloudmap>` command synchronizes the cloud map with the given the repository host.  It imports the latest changes from the host, merges those changes into the main branch of the cloud map, and then exports the cloud map back to the host. The specific steps taken are as follows:

1. Update a branch named "hosts/{host_name}/{namespace}" with the latest from the repository host.

2. Merge the host branch into "main".  If a conflict is detected, the command abort with a merge error in the cloud map repository.

For example, if a repository branch or tags was changed in both branches there will be a merge conflict. If so, manually merge the changes in the local git clones (they will be recorded on the remote branch), sync them with the cloud map, then re-run this command.

3. Update the repository host with any changes it needs to match the cloud map.

The user is responsible for pushing any changes to the upstream cloud map repository, for example:

`cd cloudmap; git --push origin main`

### Sync instances of the same host

If you are self-hosting a repository host you may have multiple physical instances of the same logical repository host, for example, staging and production instances.

You can use the cloudmap to synchronize their contents by setting the ``canonical_url`` key in the repository host's configuration. In the following example, given the ``hosts`` configuration in the section below, the following commands will push all the projects in "onecommons/blueprints" namespace from the staging instance to the production instance:

```bash
# first sync latest on staging with the cloud map
unfurl cloudmap --sync staging --namespace onecommons/blueprints

# now sync the cloud map with production
unfurl cloudmap --sync production --namespace onecommons/blueprints
```

In addition to updating those instances, the repository will have branches for each repository host recording their last known state and the main branch will have the latest from both staging and production. This way the main branch serves as the "source of truth".

## Configuration

You can configure the cloud map location and repository hosts used for synchronize in the environments section of `unfurl.yaml`.  This configuration sample illustrates the available options:

```yaml
environments:
  defaults:
    cloudmaps:
      repositories:  # location of the cloud map files
        # the default cloudmap to use if cloudmap name is omitted
        cloudmap:
          url: file:../my_cloudmap  # cloudmap git repository
          clone_root: ../repos  # optional, --clone-root option overrides this
      hosts:  # repository hosts to synchronize with
        staging:
            type: unfurl.cloud
            url: https://staging.unfurl.cloud/onecommons/
            canonical_url: https://unfurl.cloud  # optional
        production:
            type: unfurl.cloud
            url: https://unfurl.cloud/onecommons/
            # limit to projects with this visibility:
            visibility: public  # optional, --visibility option overrides this
```

## Anatomy of a cloud map

If you take a look at a {ref}`cloudmap.yaml` file, you'll find YAML that looks like:

```{literalinclude} examples/cloudmap.yaml
:language: yaml
```

The full {ref}`json schema <CloudMap Schema>` can be found {ref}`here <CloudMap Schema>` but key sections include:

### Git Repositories

The bulk of a cloud map file is in the `repositories` section, which consists of map of git repositories where the key is a {ref}`package id <package identifiers>` and includes project metadata and other repository metadata such as mirrors and forks that are not stored in git repositories themselves.

### Repository Contents

The `contains` section of a repository non-exhaustively lists that files and directories in the repository that are useful for characterizing the repository and integrating it with the other resources in the cloud map. Each entry maps a path within the repository (optionally followed by a `#<fragment>` to identify a sub-element of the file) to one or more type identifiers. Unfurl looks for files of interest when synchronizing repositories with the cloud map but entries can also be added manually. Typical examples are a `Dockerfile` used to build a container image or an `ensemble-template.yaml` that contains a cloud blueprint for deployment. Each entry in `contains` may reference an `artifact` (see below) whose URL is derived from the repository URL and the path (e.g.`ensemble-template.yaml#spec/service_template` becomes `git://.../mediawiki.git#:ensemble-template.yaml%23spec/service_template`).

### Artifacts

Most code isn't used directly, instead there is a build or packaging process to create the artifact (for example, an executable binary, a software package, or a container image) that is actually used when an application is deployed; the `artifacts` section of a cloud map lists artifacts. Artifact keys are package URLs (PURLs) for published artifacts (e.g. `pkg:oci/...`, `pkg:npm/...`) or `git://` URLs for files within a repository.

Each artifact can describe what it `instantiates` (the types or components it embodies), its build- or run-time `dependencies`, its `references` to other artifacts/repositories/services, its build- or instantiation history via `instantiated_by`, and human-readable `metadata`. Using fully qualified identifiers with package ids, artifacts can reference other repositories and artifacts in the cloud map, enabling it to be used like a package manager to resolve the dependencies and components needed to deploy an application.

### Types

The `types` section is a catalog of TOSCA type definitions referenced by artifacts and services. Each entry has a `kind` (`Component`, `Artifact`, or `Capability`), an optional list of `extends` parent types, and metadata. Types are referenced by artifacts (via `instantiates`) and by other types (via `extends`) using fully-qualified names of the form `<TypeName>@<package_id>`.

### Services and Instantiations

The `services` section lists deployed service instances reachable at a URL (with endpoints, connections, and policies), and the `instantiations` section records build, deploy, or attestation events that produced specific artifacts or services. Both sections cross-reference artifacts via `instantiated_by` / `instantiated` so the graph of "what built/deployed what" can be traversed.

## Future directions

Could cloud maps evolve into a package manager for the cloud? See <https://github.com/onecommons/cloudmap/blob/main/README.md> for more on our vision.
