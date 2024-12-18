# Changelog for Unfurl

## v1.2.0 - 2024-11-22

<small>[Compare with 1.1.0](https://github.com/onecommons/unfurl/compare/v1.1.0...v1.2.0) </small>

### Major changes

#### Browser-based UI (experimental)

This release contains an experimental local port of Unfurl Cloud's [dashboard](https://unfurl.cloud/dashboard) UI, allowing you to view and edit the blueprints and ensembles in a project.

It is enabled using the new ``--gui`` option on the [unfurl serve](https://docs.unfurl.run/cli.html#unfurl-serve) command, for example:

``unfurl serve --gui /path/to/your/project``

Functionality includes:

* Adding new deployments (ensembles) to the project and updating the YAML of existing ensembles. Edits are automatically committed to git if the git worktree is clean when the update is saved.
* Edits to environment variables are synchronized with Unfurl Cloud if the local project was cloned from Unfurl Cloud. Otherwise, environment variables are saved in the project's "unfurl.yaml" except ones marked as masked -- in that case they will be saved in "secrets/secrets.yaml" (or in "local/unfurl.yaml" if the project doesn't use vault encryption).
* When creating and editing deployments, the environment's [cloud map](https://docs.unfurl.run/cloudmap.html) (defaults to the [public one](https://github.com/onecommons/cloudmap)) is used as the catalog of blueprint and TOSCA types to search.
* Note that Unfurl's built-in web server is intended for local use only. For more robust scenarios you can run Unfurl as a WSGI app (`unfurl.server.serve:app`) and set the `UNFURL_SERVE_GUI` environment variable to enable GUI mode. Or host your project on an Unfurl Cloud instance.

#### Rust-based inference engine

Unfurl now includes a scalable (Rust-based) inference engine that tries to match node templates with missing requirements [TOSCA](https://docs.unfurl.run/tosca.html) based on a requirement's:

* node and capability types
* relationship's valid_target_types
* node_filter property and capabilities
* node_filter match expressions

Finding the match for a TOSCA requirement can't be determined with a simple algorithm. For example, a requirement node_filter's match can depend on a property value that is itself computed from a requirement (e.g. via TOSCA's ``get_property`` function). So finding a node filter match might change a property's value, which in turn could affect the node_filter match.  Similarly, the inference engine supports Unfurl's `node_filter/requirements` extension, which allows additional constraints to be applied to a target node upon match.

This extension is optional -- if the extension binary is not found or fails to load, a warning will logged and the TOSCA parser will fall-back to previous behavior. However, topologies that rely on multi-step inference to find matching requirements will fail to find them and report validation errors.

#### Documentation

Documentation at [docs.unfurl.run](https://docs.unfurl.run/) has been revamped and expanded. New [Quick Start](https://docs.unfurl.run/quickstart.html) and [Solution Overview](https://docs.unfurl.run/solution-overview.html) chapters have been added and the reference documentation in other chapters has been greatly expanded.

#### Project creation improvements

* The unfurl clone command now supports two new sources: TOSCA Cloud Service Archives (CSAR) (a file path with a .csar or .zip extension) and [cloudmap](https://docs.unfurl.run/cloudmap.html) URLs like ``cloudmap:<package_id>``, which will resolve to a git URL.

* Improved support for managing projects that keep their ensembles in separate git repositories:

  - Stop registering the ensemble with the project when its in a separate repository.
  - When creating an ensemble-only repository, add an unfurl.yaml and use that to record the default environment for the ensemble.  Add 'ensemble_repository_config' to skeleton vars when creating that file.
  - Allow unfurl.yaml files that are alongside ensemble.yaml to be nested inside another Unfurl project.
  - Cloning a remote repository containing ensembles will no longer create a new ensemble unless the clone source explicitly pointed at one.

The net effect of these changes is that the outer and inner repositories can be cloned separately without requiring manual reconfiguration.

* Support source URLs with a relative "file:" scheme and an URL fragment like `#ref:file/path` to enable remote clone semantics for local git repos.

* When adding a ensemble to an existing project, if the `--use-environment` option names an environment that doesn't exist, it will now be added to the project's unfurl.yaml.

* A blueprint project (a project created with --empty) will no longer have a vault password created unless a VAULT_PASSWORD skeleton var is set.

* You can now hard-code TOSCA topology inputs when creating an ensemble by passing skeleton vars prefixed with "input_", e.g. `--var input_foo 0`.

* Creating the unfurl home project is now a monorepo project by default (use `unfurl home` command's new `--poly` option to override).

* Custom project skeletons can now skip creating a particular file by including a 0 byte file of that name.

See the updated [documentation](https://docs.unfurl.run/projects.html#cloning-projects-and-ensembles) for more information.

#### CLI Command-line enhancements

* Colorize and format cli help output.
* `unfurl validate` will now validate cloudmap files, TOSCA service templates, and TOSCA DSL Python files.  Also added is new `--as-template` option to explicitly treat a file as a blueprint template, as opposed an ensemble (eg. like ensemble-template.yaml).
* Rename ``unfurl undeploy`` to ``unfurl teardown`` ("undeploy" stills works but is now undocumented).
* The [Job family](https://docs.unfurl.run/cli.html#unfurl-deploy-commands) of commands now accept multiple `--instance` options
* `unfurl run` adds a `--save` option.
* `unfurl home --init` now accepts the `--var`, `--skeleton`, and `--poly`x options.
* When running an unreleased version of Unfurl, `unfurl version` will append prerelease and build id segments conforming to the semantic version 2.0 syntax.

#### TOSCA Python DSL

Update the tosca package to v0.1.0, which includes the following fixes and enhancements:

New TOSCA syntax support in the Python DSL:

* Topology inputs and outputs (TopologyInputs and TopologyOutputs classes)
* Node templates with "substitute" and "select" directives:
   - Allow nodes with those directives to be constructed with missing fields.
   - Add Node.substitute() method and substitute_node() and select_node() factory functions to avoid static type errors
   - Add "_node_filter" attribute for node templates with the "select" directive.
* Serialize attributes that match the pattern "{interface_name}_default_inputs" as interface level operation inputs on TOSCA types or templates.
* Add set_operation() method for setting TOSCA operation definitions directly on template objects.
* Deployment blueprints with the DeploymentBlueprint class.
* The "concat" and "token" TOSCA functions.
* Better support for TOSCA data types that allow undeclared properties:
  - Allow EnvironmentVariables constructor to accept undeclared properties.
  - Add OpenDataType.extend() helper method to add undeclared properties.

Other DSL fixes and changes:

* loader: support "from tosca_repositories.repo import module" pattern in safe mode.
* support string type annotations containing '|'.
* loader: simplify logic and enhance private module separation.
* loader: have install() always set service template basedir.
* Rename Python base classes for TOSCA types. Deprecate old names but keep them as aliases for backwards compatibility.
* loader: execute `__init__.py` when loading packages in tosca_repositories.
* Fix instance check with ValueTypes (simple TOSCA data types).
* Make sure YAML import statements are generated for templates that reference imported types.
* Have the `to_label` family of functions evaluate arguments as needed.

YAML to Python DSL conversion improvements:

* Conversion for the new TOSCA syntax described above.
* Convert templates in the `relationship_templates` section.
* Convert operations declared directly on a template.
* Convert inline relationship templates in node templates.
* Don't include template objects in `__all__` (so importing a topology doesn't add templates to the outer topology).
* Various stylistic improvements to generated Python code.
* Introduce UNFURL_EXPORT_PYTHON_STYLE=concise environment variable to elide type annotations.

#### Jobs

* Add [remote locking](https://docs.unfurl.run/jobs.html#locking) using git lfs to prevent remote jobs from simultaneous running on the same ensemble.
* TOSCA topology inputs [can now be specified](https://docs.unfurl.run/runtime.html#topology-inputs) as part of the job command line, for example `unfurl deploy --var input_foo 1`.
* The [undeploy plan](https://docs.unfurl.run/jobs.html#undeploy-teardown) will now exclude instances whose removal would break required resources that we want to keep operational.
* If present, apply a node template's node_filter when selecting instances from external ensembles.
* When invoking `unfurl run` with `--operation` and but no filter (e.g. no `--instance`) the plan will now include all instances that implement that operation.
* [Add a "customized" field](https://docs.unfurl.run/jobs.html#applying-configuration-changes) to instance status to indicate that it has intentionally diverged from its template and the reconfigure check should be skipped.
* Save job changeset yaml in "changes" directory so it gets committed in the ensemble (without results key to avoid committing sensitive information).
* Track which dependencies were written before they were read and save that in the changeset yaml as a "writeOnly" field (improves incremental update logic).
* Reporting: improved pretty print of outputs in the job summary.

#### YAML/TOSCA Parsing

* Add `+&: anchor_name` merge directive to support merge includes with YAML aliases independent of YAML parser.
* Make repositories defined in nested includes available immediately during parse time.
* Validate that node templates and artifacts are assigned the proper TOSCA entity type (disabled unless the `UNFURL_VALIDATION_MODE` environment variable includes "types").
* Allow metadata on artifact type definitions
* CSAR support: parse metadata in TOSCA.metadata files and merge with service template YAML metadata.
* Add an optional second argument get_input TOSCA function to use as a default value if the input is missing (instead raising a validation error).
* support "key_schema" field on property definitions when type is "map".
* Support "type" and "metadata" fields on topology outputs
* Fix node_filter matching
* Fix validation of semantic versions with a build metadata segment.

#### Other

* Add support for Python 3.13
* Modernize Unfurl's Python packaging.
* loader: only search repository directories when loading secrets (for performance).
* logs: sanitize sensitive url parameters
* server: Switch built-in http server from Uvicorn to Waitress.
* ansible: have ansible-galaxy use the current Python interpreter
* artifacts: update version of community.docker ansible collection
* cloudmap: Add json-schema validation of cloudmap.yaml.
* runtime: Add a few circular topology guards.
* json export: Always include a DeploymentTemplate in blueprint and deployment formats.

## v1.1.0 - 2024-4-02

<small>[Compare with 1.0.0](https://github.com/onecommons/unfurl/compare/v1.0.0...v1.1.0) </small>

### Major changes

#### Ansible configurator

* A playbook's host is now set automatically if not explicitly specified. See <https://docs.unfurl.run/configurators.html#playbook-processing> for the selection rules.
* If `playbook` and `inventory` input parameters have a string value, detect whether to treat as a file path or parse to YAML.
* Fix rendering of `inventory.yml` when the `inventory` input parameter is set to inline YAML.
* If the `ansible_connection` host var is not explicitly set, default to "ssh" if we're also setting `ansible_host` to the node's `ip_address`.

#### Update tosca package to v0.0.8

Release includes the following fixes and enhancements:

* yaml to python: more idiomatic Python when importing ``__init__.yaml``
* yaml to python: use import namespace when following imports
* yaml to python: don't forward reference built-in types
* overwrite policy: don't overwrite if converted contents didn't change
* remove dependency on unfurl package.
* support array and key access in field projections
* allow regular data as arguments to boolean expressions.
* add `fallback(left: Optional[T], right: T) -> T` to `unfurl.tosca_plugins.expr` for type-safe default expressions.
* move `tfoutput` and `tfvar` Options to `unfurl.tosca_plugins.expr` (this make them available in safe mode).

#### Server enhancements and fixes

* Add a `/empty_cache` POST endpoint for clearing entire cache (using optional `prefix` parameter). Access requires `UNFURL_SERVER_ADMIN_PROJECT` environment variable to be set and the `auth_project` URL parameter to match it.
* Patch and export now support the "branch" field on DeploymentTemplate
* Invalidate cached blueprint export even if file in the key didn't change.

### Other Notable bug fixes

897c32ba testing: add mypy testing api and make lifecycle api more flexible

3f80e5bf job: fix for TaskView.find_connection()

269f981c runtime: refine "is resource computed" heuristic

253f55aa cloudmap: fix tag refs on gitlab hosts

61da0c6f clone: apply revision if found in fragment of the cloned url

63e2f48a kompose: have KomposeInputs use ``unfurl_datatypes_DockerContainer``

03feb7d7 plan: exclude replaced node templates from relationships

0922b557 logging: smarter truncation of log messages with stack traces

aaabb8a2 packages: fix resolving package compatibility

667c59d9 export: stop clearing out requirement match pointing to nested templates

5bfef1d8 export: fix `get_nodes_of_type` in match filters

a3c8fb54 export: stop hoisting default templates as requirements.

df2d3e97 parser: fix matching when a requirement target is substituted by the outer topology template.

a3f7feae parser: fix typename checking when evaluating 'node' field on requirements

b570246f parser: use the type's namespace when creating properties.

1b965e5b parser: fix ``Namespace.get_global_name_and_prefix()``

### Breaking changes

Drop support for Python 3.7

## v1.0.0 - 2024-2-26

<small>[Compare with 0.9.1](https://github.com/onecommons/unfurl/compare/v0.9.1...v1.0.0) </small>

### Features

We've strived to maintain backwards compatibility and API stability for a while now, so for this release we decided to go ahead and christen it 1.0 🎉.

Major new features include:

#### TOSCA namespaces and global type identifiers

This release adds features designed to enable 3rd party type libraries to be shared within the same TOSCA topology and for Unfurl API consumers (such as Unfurl Cloud) to manage them.

* Namespace isolation.

Each imported service template is placed in a separate namespace that is used to resolve type references in the file. It includes the types defined in that file along with the types it imports, with those type names prefixed if `namespace_prefix` key is set on the import definition. The namespace will be unique to that import unless an explicit namespace is declared (see below). This can be disabled or limited using the new  `UNFURL_GLOBAL_NAMESPACE_PACKAGES` environment variable (see _Breaking changes_ below). Note that the Python DSL already behaves this way, as documented [here](https://github.com/onecommons/unfurl/tree/main/tosca-package#imports-and-repositories).

* Support for TOSCA 1.3's `namespace` field

If a service template explicitly declares a namespace using the `namespace` keyword, its namespace will be assigned that name and namespace isolation will be disabled for any templates it imports -- so any import will share the same namespace unless it also declares its own namespace. In addition, any other template that declares the same namespace identifier will be placed in the same namespace. Shared namespaces means a template can reference types it didn't explicitly import and overwrite existing type definitions with the same name and so declaring namespaces is not recommended.

* Globally unique identifiers for types.

Namespaces can be used to generate globally unique type names and updated the Unfurl Server APIs and GraphQl/JSON export format to use these globally unique names.
Follow the format `<typename>@<namespace_id>` where `namespace_id` is namespace the type was declared in.  If a namespace id isn't explicitly declared using the `namespace` keyword, one will be generated using the package id of its repository or current project, and optionally, a file path if it isn't the root service template.
For example: `ContainerComputeHost@unfurl.cloud>/onecommons/std` (a type defined in `service-template.yaml`) and `EC2Instance@unfurl.cloud>/onecommons/std:aws` (a type defined in `aws.yaml`). TOSCA and unfurl types defined in the core vocabulary (and don't need to be imported) are not qualified. Built-in unfurl types that do need to be imported use `unfurl` as there package id, for example: `unfurl.nodes.Installer.Terraform@unfurl:tosca_plugins/artifacts`.
Generation of type global names with export and the APIs can be disabled by setting the `UNFURL_EXPORT_LOCALNAMES` environment variable (See _Breaking changes_ below).

* Cross-package interoperability with type metadata.

A package can declare compatibility with types in different packages without having to import those packages using the `aliases` and `deprecates` keywords in the metadata section of a type definition. The keywords value can be either a fully qualified type name or a list of fully qualified type names and indicate that the type is equivalent to the listed types. This is used both by the parser and by the API (export includes those types in exported types `extends` section) used by Unfurl Cloud's UI.

#### Unfurl Server and export APIs

* Unfurl Server (and the Unfurl Cloud front-end) patching API now uses global type names to generate import statements with prefixes as needed to prevent clashes packages with the same name.

* Support for HTTP caching: Improved etag generation and Cache-Control headers that enable the browser and proxy caches to use stale content while processing slow requests. Use the `CACHE_CONTROL_SERVE_STALE` environment variable to set.

* Add a Dockerfile that includes a nginx caching proxy in front of unfurl server. Provide prebuilt container images as `onecommons/unfurl:v1.0.0-server-cached` on docker.io and ghcr.io.

* Improvements to Redis caching: Track dependencies better, cache commit dates for more efficient shallow clones, improved error handling and recovery.

* Local developer mode will now serve content from any local repository tracked by the current project or the home project (in `local/unfurl.yaml`). It also improves handling local changes and error recovery.

* Improve type annotations for new Graphql types and consolidated Graphql Schema.

* Add support for a `property_metadata` metadata key to apply metadata to individual properties on a TOSCA datatype.

For example, this property declaration applies the `user_settable` metadata key to the environment property on unfurl.datatypes.DockerContainer:

```yaml
      container:
        type: unfurl.datatypes.DockerContainer
        metadata:
          property_metadata:
            environment:
              user_settable: true
```

#### Python DSL

Service templates written in Python now have the following integration points with Unfurl's runtime:

* ToscaType implementations of TOSCA operations can return a Python method instead of an artifact or configurator and that method will converted to a configurator.
* If TOSCA operation attempts to execute runtime-only functionality it will be invoked during the job's render phase instead of converted to YAML.
* Introduce a `Computed()` field specifier for TOSCA properties whose value is computed at runtime with the given method.
* Add a `unfurl.tosca_plugins.functions` module containing utility functions that can be executed in the safe mode Python sandbox. This allows these functions to be executed in "parse" mode (e.g. as part of a class definition or in `_class_init`).
* Add a `unfurl.expr` module containing type-safe python equivalents to Unfurl's eval expression functions.
* Add methods and free functions providing type-safes equivalents to Unfurl's query expressions: `find_configured_by`, `find_hosted_on`, `find_required_by` and `find_all_required_by` APIs.
* Providing these apis required synchronizing a job's task context as a per-thread global runtime state, add public apis for querying global state and retrieving the current `RefContext`.
* Treat non-required instance properties as optional (return None instead of raising KeyError)
* Add a public `unfurl.testing.create_runner()` api for writing unit tests for Python DSL service templates.

Various improvements to YAML-to-Python and Python-to-YAML conversion, including:

* Better support for artifacts
* Better default operation conversion
* Support for aliased references (multiple variables assigned to the same type).
* Special case module attributes named `__root__` to generate TOSCA `substitution_mappings` (aka `root`). For example:

```python
    __root__ = my_template

or

    __root__ = MyNodeType
```

DSL API improvements:

* Add a `NodeTemplateDirective` string enum for type-safe node template directives.
* Add a `@anymethod` method decorator for creating methods that can act as both a classmethods and regular method.
* Revamp Options api for typed and validated metadata on field specifiers.
* Add a `DEFAULT` sentinel value to indicate that a field should have a default value constructed from the type annotation. This helps when using forward references to types that aren't defined yet as well as prevent a bit of DRY.
* Add a similar `CONSTRAINED` sentinel value to indicate that the property's value will be set in `_class_init`.
* Add a `unfurl.support.register_custom_constraint()` API for registering custom TOSCA property constraint types.
* Add UNFURL_TEST_SAFE_LOADER environment variable to force a runtime exception if the tosca loader isn't in safe mode or to disable safe mode (for testing).UNFURL_TEST_SAFE_LOADER=never option to disable, any other non-empty value to enforce safe mode.
* Improve the [API documentation](https://docs.unfurl.run/api.html#api-for-writing-service-templates).
* Release tosca package 0.0.7

#### Packages

* Package version resolution during loading of an ensemble now honors the lock section of the ensemble if present. After an ensemble is deployed, the version tag recorded in the lock section for a package will take precedence.

* The lock section will record if a repository was missing version tags, in that case, subsequent deployments will attempt to fetch the first version tag. (As opposed to the default behavior of fetching the latest version tag if one wasn't explicitly specified.)

* Package rules are now serialized in the lock section, following the format used for `UNFURL_PACKAGE_RULES` environment variables. These rules will be applied in future unfurl invocation if the `UNFURL_PACKAGE_RULES` environment variable is missing.

* Package rules are applied more widely, for example when matching repository URLs and to namespace ids.

* A "packages" key has be added to Deployment graphql object containing the packages and versions used by the deployment.

### Artifact enhancements

Support for the following built-in keywords on artifact definitions have been added as TOSCA extensions:

* `contents`: Artifacts can now specify their contents inline using the new  field. Contents can be auto-generated using eval expression or jinja templates.
* `target`:  The node template to deploy the artifact to (default is the node template the artifact is defined in).
* `permissions`: A string field indicating the file permissions to apply when an artifact is deployed as a file.
* `order`: A number field that provides a weight for establishing the order in which declared artifacts are processed.
* `intent`: A string field for declaring the (user-defined) intent for the artifact.

* Built-in artifact fields will now be evaluated like user-defined properties at runtime (e.g. they can contain eval expressions).
* Artifact can now be dynamically created when declared in resultTemplates.

### Other Features

* Support Python 3.12

* Add an `unfurl.testing` module for unit tests.

* Update the `dashboard` skeleton to use the new `onecommons/std` repository.

* The runtime will add a default relationship of type of `unfurl.relationships.ConnectsTo.ComputeMachines` to the current environment when a primary provider isn't defined in that environment.

* Add support for a `validation` key in type metadata to provide user-defined eval expressions for validating properties and custom datatypes (equivalent to the `validation` key in property metadata).

* Add `required` and `notnull` options to the "strict" field on "eval" expressions. This provides simple validation of eval expressions, e.g.:

```yaml
    eval: .::foo
    strict: required  # raise error if no results are found
```

or

```yaml
    eval: .::foo
    strict: notnull # raise error if result isn't found or is null
```

### Breaking changes

* Prior to this release all imports shared the same namespace for types. For example, one imported file could reference a type declared in another file if both those files happened to be both imported by another service template.

With this release this behavior is only enabled if the importing template "namespace" key and the imported templates don't declare a different namespace. Otherwise, each TOSCA import now has its own namespace. Types only have visibility and templates.

If it is a package you can't modify, you can use
`UNFURL_GLOBAL_NAMESPACE_PACKAGES` expects space separated list of namespaces names with wildcards. So `UNFURL_GLOBAL_NAMESPACE_PACKAGES='*'` would enable that flag for all service templates.

If missing, defaults to "unfurl.cloud/onecommons/unfurl-types*"

* The unfurl server API and the ``export`` cli command now exports type names as fully qualified. This can be disabled by setting the ``UNFURL_EXPORT_LOCALNAMES`` environment variable. This will environment variable will be removed in a future release.

* To enable Python 3.12, Unfurl now depends on ansible-core version 2.15.9.

### Minor Enhancements and Notable Bug Fixes

* **job** If a node template is created for a "discovered" instance that has a parent, set that parent as the "host" requirement for the template.
* **job** Improve the job summary table and other logging tweaks.
* **job** Report blocked tasks separately from failed jobs (and mark more tasks as blocked).
* **plan** Improve heuristic for detecting circular dependencies and deadlocks (fixes #281)
* **job** don't silently skip operations when instantiating a configurator class fails.
* **runtime** Fix to assure the reevaluation of computed results when the computation's dependencies change.
* **cli** Add verbose output to the `status` command if `-v` flag is used.
* **cloudmap** add --repository cli option
* **cloudmap** add a `save_internal` config setting for Gitlab hosts.
* **job** Add heuristic for setting the status on relationship instances when the task's configurator doesn't explicitly set it.
* **job** add unfurl install path to the `$PATH` passed to tasks.
* **job** By default, dry run jobs print the modified ensemble.yaml to the console instead of saving to disk. Use `UNFURL_SKIP_SAVE=never` to force them to be saved.
* **parser** Remove attribute definition if a derived type re-declares it as a property.
* **parser**: Move default node template (those with a "default" directive) completely from inner topology to the root topology.
* loader, export: don't add environment instances or imports to ensemble except when generating json for the `environments`.
By default, don't instantiate environment instances unless they are imported from external ensembles. Similarly, don't include imports declared in an environment unless they have a namespace_prefix that referenced by a connection in the environment.
* **logging** add a `UNFURL_LOG_TRUNCATE` environment variable to change the maximum log message length (default: 748)
* **parser** Accept relationship types defined in TOSCA extensions in requirement definitions.
* **eval** have template function's path parameter make relative paths relative to source location
* **dns** install octodns artifact if needed
* **ansible** load ansible collection artifact as soon as they are installed.
* **kompose** make sure service names should conform to dns host name syntax
* **kompose** fix `expose` input parameter
* **kompose** implement the `env` input parameter
* **kompose** update artifact version
* **package wide** many improvements to type annotations; for example, introducing TypedDicts and overloaded signatures for the to_label family of functions.

## v0.9.1 - 2023-10-25

<small>[Compare with 0.9.0](https://github.com/onecommons/unfurl/compare/v0.9.0...v0.9.1) </small>

### Features

#### TOSCA and Python DSL

* Add `ToscaInputs` classes for Unfurl's built-in configurators to enable static type checking of configurator inputs.

* Introduce the `Options` class to enable typed metadata for TOSCA fields and have the Terraform configurator add `tfvar` and `tfoutput` options to automatically map properties and attributes to Terraform variables and outputs, respectively.

This example uses the above features to integrate a Terraform module.

```python
from unfurl.configurators.terraform import TerraformConfigurator, TerraformInputs, tfvar, tfoutput

class GenericTerraformManagedResource(tosca.nodes.Root):
    example_terraform_var: str = Property(options=tfvar)
    example_terraform_output: str = Attribute(options=tfoutput)

    @operation(apply_to=["Install.check", "Standard.configure", "Standard.delete"])
    def default(self, **kw):
        return TerraformConfigurator(TerraformInputs(main="terraform_dir"))
```

* Introduce `unfurl.datatypes.EnvironmentVariables`, a TOSCA datatype that converts to map of environment variables. Subclass this type to enable statically typed environment variables.

* Allow TOSCA data types to declare a "transform" in metadata that is applied as a property transform.

* `node_filter` improvements:
  * Recursively merge `requirements` keys in node filters when determining the node_filter for a requirement.
  * Allow `get_nodes_of_type` TOSCA function in node_filter `match` expressions.

* Release 0.0.5 version of the Python tosca package.

#### Packages

* Allow service templates to declare the unfurl version they are compatible with.

They can do this be declaring a repository for the unfurl package like so:

```yaml
        repositories:
          unfurl:
            url: https://github.com/onecommons/unfurl
            revision: v0.9.1
```

Unfurl will still resolve imports in the unfurl package using the local installed version of unfurl but it will raise an error if it isn't compatible with the version declared here.

* Do semver compatibility check for 0.* versions.

Even though pre 1.0 versions aren't expected to provide semver guarantees the alternative to doing the semver check is to treat every version as incompatible with another thus requiring every version reference to a package to be updated with each package update. This isn't very useful, especially when developing against an unstable package.

* Support file URLs in package rules.

### Minor Enhancements and Notable Bug Fixes

* **parser** allow merge keys to be optional, e.g. "+?/a/d"
* **loader**: Add a `UNFURL_OVERWRITE_POLICY` environment variable to guide the loader's python to yaml converter.
* **loader**: Relax restrictions on `from foo import *` and other bug fixes with the DSL sandbox's Python import loader.
* **init**: apply `UNFURL_SEARCH_ROOT` to unfurl project search.
* **packages**: If a package rule specifies a full url, preserve it when applying the rule.

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

Support includes an extension to TOSCA's requirements mapping that enables you to essentially parameterize an embedded template by letting the outer (the embedding template) substitute node templates in the embedded template.

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
