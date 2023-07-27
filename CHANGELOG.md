# Changelog for Unfurl

## 0.8.0 - 2023-07-26

<small>[Compare with 0.7.1](https://github.com/onecommons/unfurl/compare/v0.7.1...v0.8.0)</small>

### Breaking Changes

- The Unfurl package now only depends on the [ansible-core](https://pypi.org/project/ansible-core) package instead of the full [ansible](https://pypi.org/project/ansible/) package. Unfurl projects that depend on ansible modules installed by that package will not work with new Unfurl installations unless it is installed by some other means -- or, better, declare an Ansible collection artifact as a dependency on the template that requires it. For an example, see this usage in the [docker-template.yaml](https://github.com/onecommons/unfurl/blob/v0.7.2/unfurl/configurators/docker-template.yaml#L127C29-L127C29).

### Features

- Allow Ansible collections to be declared as TOSCA artifacts. Some predefined ones are defined [here](https://github.com/onecommons/unfurl/blob/v0.7.2/unfurl/tosca_plugins/artifacts.yaml#L163).
- Unfurl Server now tracks the dependent repositories accessed when generating cached representations (e.g. for `/export`) and uses that information to invalidate cache items when the dependencies change.
- Unfurl Server now caches more git operations, controlled by these environment variables: `CACHE_DEFAULT_PULL_TIMEOUT` (default: 120s) and
`CACHE_DEFAULT_REMOTE_TAGS_TIMEOUT` (default: 300s)
- Unfurl Server: add a `/types` endpoint that can extract types from a cloudmap.
- API: allow simpler [Configurator.run()](https://docs.unfurl.run/api.html#unfurl.configurator.Configurator.run) implementations
- cloudmap: The [cloudmap](https://docs.unfurl.run/cli.html#unfurl-cloudmap) command now supports `--import local`.
- eval: Unfurl's Jinja2 filters that are marked as safe can now be used in safe evaluation mode (currently: `eval`, `map_value`, `sensitive`, and the `to_*_label` family).

### Bug Fixes

- jobs: Fix issue where some tasks that failed during the render phase were missing from the job summary.
- jobs: Don't apply the `--dryrun` option to the tasks that are installing local artifacts. (Since they will most likely be needed to execute the tasks in the job even in dryrun mode.)
- k8s: Fix evaluation of the `kubernetes_current_namespace()` expression function outside of a job context
- k8s: Filter out data keys with null values when generating a Kubernetes Secret resource.
- helm: Fix `check` operation for `unfurl.nodes.HelmRepository`
- helm: If the Kubernetes environment has the insecure flag set, pass `--kube-insecure-skip-tls-verify` to helm.

### Misc

* Introduce CHANGELOG.md (this file -- long overdue!)
* CI: container images will be built and pushed to https://github.com/onecommons/unfurl/pkgs/container/unfurl with every git push to CI, regardless of the branch. (In addition to the container images at https://hub.docker.com/r/onecommons/unfurl which are only built from main.)
