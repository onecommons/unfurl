<p align="center">
  <img src="https://docs.unfurl.run/_images/unfurl_logo.svg" width="400px">
</p>

<p align="center">
  <a href="https://badge.fury.io/py/unfurl"><img src="https://badge.fury.io/py/unfurl.svg" alt="PyPI version" height="20"></a>
</p>

# Introduction

Unfurl is a command line tool for managing your DevOps infrastructure. Unfurl lets you easily track configuration, secrets, software and code dependencies, and deployment history all in git.

Unfurl can integrate with the DevOps tools you are already using -- like Terraform, Ansible, and Helm -- allowing you to encapsulate your DevOps processes into reusable building blocks and describe your cloud infrastructure in simple, application-centric terms.

## Our Vision

The ultimate goal of Unfurl is enable anyone to clone, fork, and deploy live cloud services as easily as cloning and building code from git. So that we can have:

- Fully-functional and reproducible archives of web applications.
- Location independence to decentralize cloud infrastructure.
- Cooperatively build and run cloud services the same way we build open source software.

## How it works

1\. Use `unfurl init` to create an Unfurl-managed git repository. Or use `unfurl clone` to clone an existing one.

2\. The repository will contain a few YAML files that you can edit. They will describe everything you'll need to deploy your application, such as:

- Cloud provider and SaaS services account credentials and other secrets organized into environments.
- Code repositories and container image registries.
- A high-level model of your cloud infrastructure and their dependencies such as compute instances and databases, described using the [OASIS TOSCA](https://www.oasis-open.org/committees/tc_home.php?wg_abbrev=tosca) (Topology and Orchestration Specification for Cloud Applications) standard.
- Operations that invoke Terraform, Ansible, or other command-line tools (which Unfurl can automatically install).

3\. Use `unfurl deploy` to deploy the infrastructure. Unfurl will generate a plan based on your target environment and high-level model and choose the correct operations to call. It will commit to git the latest configuration and a history of changes to your cloud accounts.

4\. Now you have a reproducible description of your cloud infrastructure stored in git! So you can:

- Push your repository to a git service such as Github or Gitlab to share it. For access control, each environment can be stored as separate git submodules or branches.
- Pull incoming changes and review and approve pull requests before deploying.
- Clone the repository and deploy to new environments even if they use different services -- because is your model is adaptable, manual changes are minimized.

## Features

### No server, agentless

Simple, stand-alone CLI that can be used both in your local development environment or in an automated CI/CD pipeline.

### Deploy infrastructure from simple, application-centric descriptions

- Model your cloud infrastructure with [OASIS TOSCA](https://www.oasis-open.org/committees/tc_home.php?wg_abbrev=tosca) (Topology and Orchestration Specification for Cloud Applications) standard YAML vocabulary.
- Import reusable and adaptable components or build (and publish) your own.
- Easily declare dependencies to enable incremental deployment.
- Path-based query DSL to express dynamic relationships between resources and configurations
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
  - Syntactic macros for YAML provide generic facility for re-use and avoiding verbose, boiler-plate

### Zero installation

- Manage your local machine and bootstrap setup by deploying locally.
- Downloads and installs specified software versions and code as deployment process.
- Creates and manages isolated deployment environments
- Isolated environments can be either a Docker container or a Python virtualenv with [asdf](https://asdf-vm.com/).
- Clear separation of local and shared configuration to avoid unnecessary local configuration steps.

### Flexible Secrets Management

- Declare secrets and protect your repository.
- Inline encryption or use external secret managers like HashiCorp Vault.
- Automatic encryption of files in `secrets` folders.
- Sensitive content redacted in output and logs

### "Day Two" Operations

- Check, discover and repair commands
- Define your own workflows for maintenance tasks like backup and restore.

# Installation

`unfurl` is available on [PyPI](https://pypi.org/project/unfurl/). You can install using `pip` (or `pip3`):

```
pip install unfurl
```

By default `unfurl` creates a virtual Python environment to run in so it only installs the minimal requirements needed to run the command line. If you want to run it using your system Python install it with the "full" option:

```
pip install unfurl[full]
```

You can also install `unfurl` directly from this repository to get the latest code:

```
pip3 install "git+https://github.com/onecommons/unfurl.git#egg=unfurl"
```

Alternatively, you can use the [Unfurl container on Docker Hub](https://hub.docker.com/r/onecommons/unfurl):

```
docker run --rm -it -v $(pwd):/data -w /data onecommons/unfurl:stable unfurl ...
```

The `stable` tag matches the version published to PyPi; `latest` is the latest code from the repository.

## Requirements

- Linux or MacOS
- Git
- Python (3.7, 3.8, 3.9, 3.10 or 3.11)

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

## Developing

```
git clone --recurse-submodules https://github.com/onecommons/unfurl
```

To build documentation: Run `tox -e docs`.

To build a distribution package run:

```
python setup.py sdist bdist_wheel
```

You can now install this package with pip, for example:

```
pip install ./dist/unfurl-0.2.2.dev3-py2.py3-none-any.whl
```

## Running unit tests

You can use `tox` to run the unit tests inside the supported python environments with the latest source installed.
Install tox `pip install tox==3.28.0` and then run `tox` in source root. To install the dependencies you may need header files installed by the following OS packages: `python-dev`, `libcrypt-dev`, `openssl-dev`. (Note: if installation of a dependency fails, reinvoke `tox` with `-r` to recreate the test environment.)
If you use `asdf` to manage multiple versions of Python, also install `tox-asdf`: `pip install tox-asdf`.

Arguments after `--` are passed to the test runner, e.g. to run an individual test: `tox -- tests/test_runtime.py`.

## Status and Caveats

Unfurl is in early stages of development and should not be used in production. In particular be mindful of these limitations:

- Only clone and deploy trusted repositories and projects. The docker runtime is not configured to provide isolation so you should assume any project may contain executable code that can gain full access to your system.
- Locking to prevent multiple instances of Unfurl from modifying the same resources at the same time currently only works with instances accessing the same local copy of an ensemble.
- Incremental updates are only partially implemented. You can incrementally update an ensemble by explicitly limiting jobs with the `--force` and `--instance` [command line options](https://docs.unfurl.run/cli.html#unfurl-deploy).

## Unfurl Cloud

The best way to manage your Unfurl project is to use [Unfurl Cloud](https://unfurl.cloud), our open-source platform for collaboratively developing cloud applications.

## Get Started

Check out the rest of Unfurl's documentation [here](https://docs.unfurl.run/quickstart.html)
