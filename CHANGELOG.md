# Changelog for Unfurl

## v0.9.0 - Friday 10-13-23

<small>[Compare with 0.8.0](https://github.com/onecommons/unfurl/compare/v0.8.0...v0.9.0) </small>

### Features

#### ***Introduce Python DSL for TOSCA***

Write TOSCA as Python modules instead of YAML. Features include:

* Static type checking of your TOSCA model.
* IDE integration.
* Export command now support conversion from YAML to Python and Python to YAML.
* Python data model simplifies TOSCA YAML
* But allows advanced constraints that encapsulate verbose relationships and node filters.
* Python executes in a sandbox to safely parse untrusted TOSCA service templates.

See <https://github.com/onecommons/unfurl/blob/main/tosca-package/README.md> for more information.

#### Unfurl Cloud local development mode

You can now see local changes to a blueprint project under development on [Unfurl Cloud](https://unfurl.cloud) by running `unfurl serve .` in the project directory.  If the project was cloned from Unfurl Cloud, it will connect to that local server to render and deploy that local copy of the blueprint (for security, on your local browser only). Use the `--cloud-server` option to specify an alternative instance of Unfurl Cloud.

### Embedded Blueprints (TOSCA substitution mapping)

Support for TOSCA substitution mapping has been stabilized and integrated into Unfurl Cloud.

One new feature level enhancement an extension to TOSCA's requirements mapping to that enables you to essentially parameterize an embedded template by letting the outer (the embedding template) substitute node templates in the embedded template.

When substituted node template (in the outer topologies) declares requirements with whose name matches the name of a node template in the substituted (inner) topology then that node template will be replaced by the node template targeted by the requirement.

For example, if the substituted (inner) topology looked like:

```yaml
node_types:
  NestedWithPlaceHolder:
    derived_from: tosca:Root
    requirements:
    - placeholder:
        node: PlaceHolderType

topology_template:
   substitution_mapping:
      node_type: NestedWithPlaceHolder

  node_templates:
      placeholder:
        type: PlaceHolderType
      ...
```

Then another topology that is embedding it can replace the "placeholder" template like so:

```yaml
node_templates:  
    nested1:
      type: NestedWithPlaceHolder
      directives:
      - substitute
      requirements:
        - placeholder: replacement

    replacement:
      type: PlaceHolderType
      ...
```

#### CLI Improvements

* Add `--skip-upstream-check` global option which skips pulling latest upstream changes from existing repositories and checking remote repositories for version tags.

Improvements to sub commands:

* **init** Add Azure and Kubernetes project skeletons (`unfurl init --skeleton k8s` and `unfurl init --skeleton azure`)
* **clone** Add `--design` flag which to configure the cloned project for blueprint development.
* **serve** Add `--cloud-server` option to specify Unfurl Cloud instance to connect to.
* **export** Add support for exporting TOSCA YAML to Python and Python to YAML; add `--overwrite` and `--python-target` options.
* **cloudmap** Also allow host urls for options that had only accepted a pre-configured name of a repository host.

#### Dry Run improvements

* When deploying jobs with `--dryrun` if a `Mock` operation is defined for a node type or template it will be invoked if the configurator doesn't support dry run mode.
* The **terraform** configurator now supports `dryrun_mode` and `dryrun_outputs` input parameters.

#### Runtime Eval Expressions

* Eval expressions can now be used in `node_filter`s to query for node matches using the new `match` keyword in the node filter.
* Add a ".hosted_on" key to eval expressions  that (recursively) follows ".targets" filtered by the `HostedOn` relationship, even across topology boundaries.
* Add optional `wantList` parameter to the jinja2 `eval`  filter to guarantee the result is a list e.g. `{{"expression" | eval(wantList=True)}}`
* The `trace` keyword in eval expressions now accept `break` as a value. This will invoke Python's `breakpoint()` function when the expression is evaluated.

#### New Environment Variables

* `UNFURL_SEARCH_ROOT` environment variable: When search for ensembles and unfurl projects Unfurl will stop when it reach the directory this is set to.

* `UNFURL_SKIP_SAVE` environment variable: If set, skips saving the ensemble.yaml to disk after a job runs,

### Minor Enhancements and Notable Bug Fixes

* **artifacts:** Add an `asdf` artifact for `kompose` and have the **kompose** configurator schedule the artifact if `kompose` is missing.
* **parser:** treat import failures are fatal errors (abort parsing and validation).
* **parser:** better syntax validation of `+include` statements.
* **cloudmap:** allow anonymous connections to Gitlab and Unfurl Cloud api for readonly access
* **plan:** fix spurious validation failures when creating initial environment variable rules
* **tosca:**: Add support for 'unsupported', 'deprecated', and 'removed' property statuses.
* **tosca:** Add support for bitrate scalar units.
* **tosca:** fix built-in storage type definition
* **plan:** fix candidate status check when deleting instances.
* **server:** make sure the generated local unfurl.yaml exists when patching deployments.
* **packages:** warn when remote lookup of version tags fails.
* **repo:** fix matching local paths with repository paths.
* **loader:** fix relative path lookups when processing an +include directives during a TOSCA import of a file in a repository.
* **logging:** improve readability when pretty printing dictionaries.

## 0.8.0 - 2023-07-26

<small>[Compare with 0.7.1](https://github.com/onecommons/unfurl/compare/v0.7.1...v0.8.0)</small>

### Breaking Changes

* The Unfurl package now only depends on the [ansible-core](https://pypi.org/project/ansible-core) package instead of the full [ansible](https://pypi.org/project/ansible/) package. Unfurl projects that depend on ansible modules installed by that package will not work with new Unfurl installations unless it is installed by some other means -- or, better, declare an Ansible collection artifact as a dependency on the template that requires it. For an example, see this usage in the [docker-template.yaml](https://github.com/onecommons/unfurl/blob/v0.7.2/unfurl/configurators/docker-template.yaml#L127C29-L127C29).

### Features

* Allow Ansible collections to be declared as TOSCA artifacts. Some predefined ones are defined [here](https://github.com/onecommons/unfurl/blob/v0.7.2/unfurl/tosca_plugins/artifacts.yaml#L163).
* Unfurl Server now tracks the dependent repositories accessed when generating cached representations (e.g. for `/export`) and uses that information to invalidate cache items when the dependencies change.
* Unfurl Server now caches more git operations, controlled by these environment variables: `CACHE_DEFAULT_PULL_TIMEOUT` (default: 120s) and
`CACHE_DEFAULT_REMOTE_TAGS_TIMEOUT` (default: 300s)
* Unfurl Server: add a `/types` endpoint that can extract types from a cloudmap.
* API: allow simpler [Configurator.run()](https://docs.unfurl.run/api.html#unfurl.configurator.Configurator.run) implementations
* cloudmap: The [cloudmap](https://docs.unfurl.run/cli.html#unfurl-cloudmap) command now supports `--import local`.
* eval: Unfurl's Jinja2 filters that are marked as safe can now be used in safe evaluation mode (currently: `eval`, `map_value`, `sensitive`, and the `to_*_label` family).

### Bug Fixes

* jobs: Fix issue where some tasks that failed during the render phase were missing from the job summary.
* jobs: Don't apply the `--dryrun` option to the tasks that are installing local artifacts. (Since they will most likely be needed to execute the tasks in the job even in dryrun mode.)
* k8s: Fix evaluation of the `kubernetes_current_namespace()` expression function outside of a job context
* k8s: Filter out data keys with null values when generating a Kubernetes Secret resource.
* helm: Fix `check` operation for `unfurl.nodes.HelmRepository`
* helm: If the Kubernetes environment has the insecure flag set, pass `--kube-insecure-skip-tls-verify` to helm.

### Misc

* Introduce CHANGELOG.md (this file -- long overdue!)
* CI: container images will be built and pushed to <https://github.com/onecommons/unfurl/pkgs/container/unfurl> with every git push to CI, regardless of the branch. (In addition to the container images at <https://hub.docker.com/r/onecommons/unfurl>, which are only built from main.)
