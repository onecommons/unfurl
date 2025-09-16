<p align="center">
  <img src="https://docs.unfurl.run/_images/unfurl_logo.svg" width="400px">
</p>

<p align="center">
  <a href="https://badge.fury.io/py/unfurl"><img src="https://badge.fury.io/py/unfurl.svg" alt="PyPI version" height="20"></a>
</p>

# Introduction

Unfurl is a command line tool for deploying services and applications.

Describe your application architecture at a high-level using the [OASIS TOSCA](https://www.oasis-open.org/committees/tc_home.php?wg_abbrev=tosca) standard and Unfurl creates a plan adapted to your deployment environment (e.g. your cloud provider, Kubernetes, or self-hosted machines), using artifacts such as shell scripts, Terraform modules or Ansible playbooks.

After deployment, Unfurl records in git all the information needed for a reproducible deployment: the configuration, the artifacts and source repositories used in deployment, as well the state of the deployed resources. This enables intelligent updates as your configuration, dependencies, or environment changes.

The best way to manage your Unfurl project is to use [Unfurl Cloud](https://unfurl.cloud), our open-source platform for collaboratively developing cloud applications.

## Why use Unfurl?

* You are developing an application consisting of several interconnected services. Even a relatively simple web application has many dependencies that cut across its stack and development and deployment processes. Unfurl can help [manage that complexity without complicated DevOps processes](https://www.unfurl.cloud/blog/why-unfurl).

* You have users that want to deploy your application on a variety of different cloud providers and self-hosted configurations. Support them by adding an adaptable blueprint to your source repository or publish it in a [Cloud Map](https://docs.unfurl.run/cloudmap.html) or on [Unfurl Cloud](https://unfurl.cloud), our open-source deployment platform.

* You want to manage configuration and live resources just like code, using the same development processes -- like pull requests and CI pipelines -- without costly server infrastructure or having to rely on manual and proprietary admin UI.

## How it works

1. Create a project to manage your deployments. 

Unfurl projects provide an easy way to track and organize all that goes into deploying an application, including configuration, secrets, and artifacts -- and track them in git. The goal is to provide isolation and reproducibility while maintaining human-scale, application-centric view of your resources.

2. Create a cloud blueprint

Avoid tedious and error prone low-level configuration by creating a cloud blueprint that describes your cloud application's architecture at a high-level in Python or YAML using the [OASIS TOSCA](https://www.oasis-open.org/committees/tc_home.php?wg_abbrev=tosca) (Topology and Orchestration Specification for Cloud Applications) standard. Blueprints includes the resources (e.g. compute instances), the services (e.g. a database) and the artifacts (e.g. a Docker container image or a software package).

3. Instantiate your blueprint

Unfurl instantiates your blueprint by building a deployment plan using implementations appropriate for the environment you are deploying into. For predefined types you can use our [Unfurl Cloud Standard library](https://unfurl.cloud/onecommons/std) for common cloud providers, kubernetes, and self-hosted instances.  Or your can easily use your own implementations -- Unfurl integrates with tools like Terraform, Ansible, and Helm or you can encapsulate the scripts you already have.

4. Deploy and Manage. 

Run `unfurl deploy` to deploy the infrastructure. It will commit to git the latest configuration and a history of changes to your cloud accounts.

After the initial deployment, subsequent deployment plans take into account the current state of its resources. This way you can reduce errors by automatically reconfiguring as dependencies and environments change. You can also create custom operations or run ad-hoc commands in the ensemble's environment.

5. Share and Collaborate

Unfurl stores everything in git so git is all you need to share projects and collaboratively manage deployments.  Unfurl can use git lfs to provide locking during deployment and can automatically encrypts secrets. Or for ease of use and more advanced needs you can use [unfurl cloud](https://unfurl.cloud) to host and deploy.

For complex applications and to enable open-source development and the integration of 3rd-party providers, Unfurl supports [design patterns](https://docs.unfurl.run/solution-overview.html#large-scale-collaboration) for integrating components and services that can be developed and deployed independently, such as [encapsulation](https://docs.unfurl.run/CHANGELOG.html#tosca-namespaces-and-global-type-identifiers), [composition](https://docs.unfurl.run/toscaref/spec-substitution-mapping.html), [loosely-coupled components](https://github.com/onecommons/unfurl/blob/main/rust/README.md), dependency injection, [semantic versioning](https://docs.unfurl.run/packages.html#package-resolution), and [component catalogs](https://docs.unfurl.run/cloudmap.html).

## Our Vision

The ultimate goal of Unfurl is enable anyone to clone, fork, and deploy live cloud services as easily as cloning and building code from git. So that we can have:

- Fully-functional and reproducible archives of web applications.
- Location independence to decentralize cloud infrastructure.
- Cooperatively build and run cloud services the same way we build open source software.

## Features

### No server, agentless

Simple, stand-alone CLI that can be used both in your local development environment or in an automated CI/CD pipeline.

### Deploy infrastructure from simple, application-centric descriptions

- Model your cloud infrastructure with the [OASIS TOSCA](https://www.oasis-open.org/committees/tc_home.php?wg_abbrev=tosca) (Topology and Orchestration Specification for Cloud Applications) standard either in YAML or [Python](https://github.com/onecommons/unfurl/blob/main/tosca-package/README.md).
- Import reusable and adaptable components or build (and publish) your own.
- Easily declare dependencies to enable incremental deployment.
- Path-based query DSL to express dynamic relationships between resources and configurations.
- Ansible-compatible Jinja2 templates provide an extensive, mature templating library.
- Dynamic matching and substitution so models can adapt to different environments, services and cloud providers.

### Integrates with the DevOps tools you are already using

- Includes out-of-the box support for:
  - [Terraform](https://docs.unfurl.run/configurators.html#terraform)
  - [Ansible](https://docs.unfurl.run/configurators.html#ansible)
  - [shell](https://docs.unfurl.run/configurators.html#shell)
  - [Helm](https://docs.unfurl.run/configurators.html#helm)
  - [Octodns](https://docs.unfurl.run/configurators.html#dns)
  - [Kubernetes](https://docs.unfurl.run/configurators.html#kubernetes)
  - [Docker](https://docs.unfurl.run/configurators.html#docker)
  - [Supervisor](https://docs.unfurl.run/configurators.html#supervisor)
- Plugin interface for adding your own.
- API for dynamic configuration enables full control over plan generation.

### Developer-friendly state management

- All resource state is stored in git as human-readable and editable, application-centric resource state
- You so can easily override or repair resource representations enabling interactive development.
- Editor friendly config files:
  - Comments, order, and whitespace are preserved.
  - Syntactic macros for YAML provide generic facility for re-use and avoiding verbose boiler-plate.

### Zero installation

- Manage your local machine and bootstrap setup by deploying locally.
- Downloads and installs specified software versions and code as deployment process.
- Creates and manages isolated deployment environments.
- Isolated environments can be either a Docker container or a Python virtualenv with [asdf](https://asdf-vm.com/).
- Clear separation of local and shared configuration to avoid unnecessary local configuration steps.

### Flexible Secrets Management

- Declare secrets and protect your repository.
- Inline encryption or use external secret managers like HashiCorp Vault.
- Automatic encryption of files in `secrets` folders.
- Sensitive content redacted in output and logs

### "Day Two" Operations

- Check, discover, and repair commands
- Define your own workflows for maintenance tasks like backup and restore.

# Installation

`unfurl` is available on [PyPI](https://pypi.org/project/unfurl/). You can install using `pipx` or `pip`:

```
pipx install unfurl
```

By default, Unfurl creates an [Unfurl home](https://docs.unfurl.run/projects.html#unfurl-home) project with a virtual Python environment for deployments so Unfurl only installs the minimal requirements needed to run the command line. If you want to deploy in the same environment, install it with the "full" option:

```
pipx install unfurl[full]
```

You can also install `unfurl` directly from this repository to get the latest code:

```
pipx install "git+https://github.com/onecommons/unfurl.git#egg=unfurl"
```

Alternatively, you can get the Unfurl container image from [GitHub](https://github.com/onecommons/unfurl/pkgs/container/unfurl) or [Docker Hub](https://hub.docker.com/r/onecommons/unfurl) and run Unfurl from the image:

```
docker run --rm -it -v $(pwd):/data -w /data onecommons/unfurl:stable unfurl ...
```

The `stable` tag matches the version published to PyPi; `latest` is the latest code from the repository.

## Requirements

- Linux or MacOS
- Git
- Python (3.9 - 3.14)

Optional: Docker or Podman

## Shell autocomplete

Use the table below to activate shell autocompletion for the `unfurl`:

| Shell | Instructions                                          |
| ----- | ----------------------------------------------------- |
| Bash  | Add this to `~/.bashrc`:                              |
|       | `eval "$(_UNFURL_COMPLETE=bash_source unfurl)"`       |
| Zsh   | Add this to `~/.zshrc`:                               |
|       | `eval "$(_UNFURL_COMPLETE=zsh_source unfurl)"`        |
| Fish  | Add this to `~/.config/fish/completions/unfurl.fish`: |
|       | `eval (env _UNFURL_COMPLETE=fish_source unfurl)`      |

## Development

To hack on Unfurl:

1. Clone this repository with its submodules:

`git clone --recurse-submodules https://github.com/onecommons/unfurl /path/to/unfurl/`

2. Install package dependencies and run Unfurl from source:

`pip install -U -e /path/to/unfurl`

3. Build the Rust extension (this is optional for developing or running Unfurl):

```
pip install setuptools-rust>=1.7.0
python setup.py build_rust --debug --inplace
```

Requires Rust >= 1.70.

4. To build documentation: Install tox (see below). Run `tox -e docs`.

5. To build a distribution package run:

```
pip install build
python -m build /path/to/unfurl/
```

You can now install this package with pip or pipx, for example:

```
pipx install ./dist/unfurl-1.1.1.dev219-cp38-abi3-macosx_12_0_arm64.whl
```

## Running unit tests

You can use `tox` to run the unit tests inside the supported python environments with the latest source installed.
Install tox `pip install tox==3.28.0` and then run `tox` in source root. To install the dependencies you may need header files installed by the following OS packages: `python-dev`, `libcrypt-dev`, `openssl-dev`. (Note: if installation of a dependency fails, reinvoke `tox` with `-r` to recreate the test environment.)
If you use `asdf` to manage multiple versions of Python, also install `tox-asdf`: `pip install tox-asdf`.

`./smoketest.sh` runs a subset of the tests. To run a specific test file, use the following command, for example:

```
tox -e py313 tests/test_runtime.py
```

Where `-e` specifies one of the environments defined in `tox.ini` (e.g. `py313`, `py39`, etc.) Arguments after `--` are passed to the test runner, e.g. to pass arguments to pytest: `tox -e py313 tests/test_runtime.py -- -s `.

When running unit tests, `tox` it will try to build the Rust extension first. To skip building and running associated tests, set the UNFURL_TEST_SKIP_BUILD_RUST=1 environment variable.

## Status and Caveats

Be mindful of these limitations:

- Only clone and deploy trusted repositories and projects. The docker runtime is not configured to provide isolation so you should assume any project may contain executable code that can gain full access to your system.
- Incremental updates are only partially implemented. You can incrementally update an ensemble by explicitly limiting jobs with the `--force` and `--instance` [command line options](https://docs.unfurl.run/cli.html#unfurl-deploy).

## Unfurl Cloud

The best way to manage your Unfurl project is to use [Unfurl Cloud](https://unfurl.cloud), our open-source platform for collaboratively developing cloud applications.

## Get Started

Check out the rest of Unfurl's documentation [here](https://docs.unfurl.run/quickstart.html). Release notes can be found [here](https://github.com/onecommons/unfurl/blob/main/CHANGELOG.md).
