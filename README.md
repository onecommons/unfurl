<p align="center">
  <img src="https://docs.unfurl.run/_images/unfurl_logo.svg" width="400px">
</p>

<p align="center">
  <a href="https://badge.fury.io/py/unfurl"><img src="https://badge.fury.io/py/unfurl.svg" alt="PyPI version" height="20"></a>
</p>

# Introduction

Unfurl is a command line tool for deploying services and applications.

Use Unfurl to describe your application architecture in terms high level resources (e.g. compute) and services. Then Unfurl creates a deployment plan adapted to your deployment environment (e.g your cloud provider, or Kubernetes), using artifacts such as shell scripts, Terraform modules or Ansible playbooks.

After deployment, Unfurl records in git all the info you need for a reproducible deployment: the configuration, the artifacts used in deployment, their versions and code dependencies, and deploy, records history, as well the state of the deployment resources. This enables intelligent updates as your dependencies or environment changes.

The best way to manage your Unfurl project is to use [Unfurl Cloud](https://unfurl.cloud), our open-source platform for collaboratively developing cloud applications.

## Why use Unfurl?

* You are developing an application with several distinct but interconnected services. Even a relatively simple web application has many dependencies that cut across its stack and development and deployment processes.  Unfurl can manage that complexity without complicated DevOps processes -- see https://www.unfurl.cloud/blog/why-unfurl for more.

* You have users that want to deploy your application on a variety of different cloud providers and self-hosted configurations. Just publish blueprint in an `.unfurl` folder or web UI on unfurl.cloud, our open-source deployment platform.

* Collaborative, open without depending on server infrastructure. Use git workflow, pull requests, while provide secret management and limiting admin access.

## How it works

1. Create a project to manage your deployments. 

Unfurl projects provide an easy way to track and organize all that goes into deploying an application, including configuration, secrets, and artifacts -- and track them in git. The goal is to provide isolation and reproducibility while maintaining human-scale, application-centric view of your resources.

2. Create an application blueprint

A blueprint that describe your cloud application's architecture in terms its resources it consumes (e.g. compute instances), the services it requires (e.g. a database) and the artifacts it consists of (e.g. a Docker container image or a software package).

Avoid tedious and error prone low-level configuration by higher-level components with a blueprint.

python, yaml, or UI described using the [OASIS TOSCA](https://www.oasis-open.org/committees/tc_home.php?wg_abbrev=tosca) (Topology and Orchestration Specification for Cloud Applications) standard.

3. Instantiate your blueprint: adapt plan render

Unfurl instantiates a blueprint by building a deployment plan using implementations appropriate for the environment you are deploying into.
For predefined types you can use our [Unfurl Cloud Standard library](https://unfurl.cloud/onecommons/std) for common cloud providers, kubernetes, and self-hosted instances.
Or if you need to define your own implementations, its easy use the tools you already use and encapsulate the scripts you already have. Operations that invoke Terraform, Ansible, or other command-line tools. 

And Unfurl can automatically install missing dependencies.

Unfurl will generate a plan and render (e.g Terraform modules or ansible playbooks) that you can review or even create a pull request.

4. Deploy and manage. Deploy Use `unfurl deploy` to deploy the infrastructure.  It will commit to git the latest configuration and a history of changes to your cloud accounts.

After the initial deployment, subsequent deployment plans take into account the current state of its resources.

This way you can reduce errors by automatically reconfiguring as dependencies and environments change. 

You can also create custom operations or run ad-hoc commands in the ensemble's environment.

5. Share and Collaborate

Unfurl stores everything in git so git is all you need to share projects and collaboratively manage deployments.  Unfurl can use git lfs to provide locking during deployment and automatically encrypts secrets. Or for ease of use and more advanced needs you can use [unfurl cloud](https://unfurl.cloud) to host and deploy.

For complex applications and to enable open-source development and the integration of 3rd-party providers, Unfurl supports design patterns for 
for integrating components and services that can be developed and deployed independently, such as encapsulation (imports), composition (substitute mapping), loosely-coupled components (dsl constraints), dependency injection (deployment blueprint / substitution mapping), modular architectures (std), semantic versioning (packaging), and component catalogs (cloudmaps).

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

`unfurl` is available on [PyPI](https://pypi.org/project/unfurl/). You can install using `pip` (or `pip3`):

```
pip install unfurl
```

Running `unfurl home --init` creates a virtual Python environment to run unfurl in so by default unfurl only installs the minimal requirements needed to run the command line. If you want to run unfurl using your system Python install it with the "full" option:

```
pip install unfurl[full]
```

You can also install `unfurl` directly from this repository to get the latest code:

```
pip3 install "git+https://github.com/onecommons/unfurl.git#egg=unfurl"
```

Alternatively, you can get the Unfurl container image from [GitHub](https://github.com/onecommons/unfurl/pkgs/container/unfurl) or [Docker Hub](https://hub.docker.com/r/onecommons
/unfurl) and run Unfurl from the image:


```
docker run --rm -it -v $(pwd):/data -w /data onecommons/unfurl:stable unfurl ...
```

The `stable` tag matches the version published to PyPi; `latest` is the latest code from the repository.

## Requirements

- Linux or MacOS
- Git
- Python (3.8, 3.9, 3.10, 3.11, 3.12)

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
git clone --recurse-submodules https://github.com/onecommons/unfurl /path/to/unfurl/

pip3 install -U -e git+file:///path/to/unfurl/
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

Be mindful of these limitations:

- Only clone and deploy trusted repositories and projects. The docker runtime is not configured to provide isolation so you should assume any project may contain executable code that can gain full access to your system.
- Incremental updates are only partially implemented. You can incrementally update an ensemble by explicitly limiting jobs with the `--force` and `--instance` [command line options](https://docs.unfurl.run/cli.html#unfurl-deploy).

## Unfurl Cloud

The best way to manage your Unfurl project is to use [Unfurl Cloud](https://unfurl.cloud), our open-source platform for collaboratively developing cloud applications.

## Get Started

Check out the rest of Unfurl's documentation [here](https://docs.unfurl.run/quickstart.html). Release notes can be found [here](https://github.com/onecommons/unfurl/blob/main/CHANGELOG.md).
