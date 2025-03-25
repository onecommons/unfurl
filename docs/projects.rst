===============
Unfurl Projects
===============

.. contents::
   :local:
   :depth: 1

Introduction
============

Unfurl uses project to manage your deployments and blueprints -- for a high-level view, see the this `oveview<Step 1: Create an Unfurl Project>`.

An Unfurl project is a directory containing an `unfurl.yaml` file.
Project can contain the following:

- blueprints: ensemble-template.yaml and 

- ensembles: representation of a deployment

- `environments` (defined in `unfurl.yaml`)

- `git repositories`

- an `execution runtime`

Project files
=============

Unfurl relies on a few file naming conventions to define a project:

* `unfurl.yaml` is a project's main configuration file and is used as a marker to indicate that a directory contains an Unfurl project. 
* Each ensemble lives its own directory. The default name for its manifest file is `ensemble.yaml`.
* ``ensemble-template.yaml`` defines the blueprint that is shared across ensembles in this project. It contains or includes the TOSCA service template.

The :cli:`unfurl init<unfurl-init>` command uses a `project skeletons` to generate the project's initial files. Other files may be included via `YAML merge directives` declared in those core files. For example, after running ``unfurl init --mono`` the new project's directory structure will look like this:

| ├── .git
| │   └── info/exclude
| │   └── ... (other git stuff)
| ├── .gitignore
| ├── .gitattributes
| ├── unfurl.yaml
| ├── secrets
| │   └── secrets.yaml
| ├── local
| │   └── unfurl.yaml
| ├── .unfurl-local-template.yaml
| ├── ensemble-template.yaml
| ├── service_template.py
| ├── ensemble
| │   └── ensemble.yaml

In the folder structure above:

- ``unfurl.yaml`` is the Unfurl project configuration file.
- ``local/unfurl.yaml`` is included by the parent ``unfurl.yaml``
- Any files in``local`` is excluded from the git repository
  and is where you'll put local or private settings you don't want to commit.
- Any files in ``secrets`` is automatically encrypted before committed.
- ``secrets/secrets.yaml`` is included by ``unfurl.yaml``.
- ``.unfurl-local-template.yaml`` is the template used generate a new ``local/unfurl.yaml`` when the project is cloned.
- ``ensemble-template.yaml`` is a template that is shared across ensembles in this project.
- ``service_template.py`` is included by ``ensemble-template.yaml`` and allows you to also declare your TOSCA as Python code
- ``ensemble`` is the folder that contains the default ensemble
  (use the ``--empty`` flag to skip creating this).
- ``ensemble/ensemble.yaml`` is the manifest file for this ensemble where the directory name is the name of the ensemble (defaults to "ensemble").
- Nested repository folders (like ``ensemble`` unless ``--mono`` flag was used) are listed in ``.git/info/exclude``

In addition, when a job runs it will add files to the ``ensemble`` folder -- see `Generated Files`.

Git Repositories
================

Unfurl can create one or more git repositories when creating a project. Unfurl provides flexibility on how to map this layout to git repositories, supporting both "monorepo" and "polyrepo" arrangements. A project can also manage git repositories declared in a blueprint or 
environment -- see `Repositories` for more information.

We can refer to git repositories by their contents:

Blueprint Repository
----------------------

A repository that contains specification files (e.g. ensemble-template.yaml) but no ensembles. Created using ``unfurl init --empty``.

Ensemble Repository
---------------------

A repository containing an ensemble and its deployment state but not its blueprint -- the blueprint is imported from a separate blueprint repository.

By default, :cli:`unfurl init<unfurl-init>` creates a blueprint repository and an ensemble repository nested inside it (but the ``--empty``, ``--mono``, ``--existing``, ``--submodule``, and ``--shared-repository`` options all modify this behavior).  This allows a project's blueprints and deployments to have separate histories and access control.

Mono Repository
---------------

Project blueprints and one or more ensembles all in one git repository.  Use ``unfurl init --mono`` to create one.

Environment repositories
-------------------------

You can create a repository that contains all the ensembles deployed into a particular environment, even if the ensembles are created in different projects. This let's you keep all the changes to a particular cloud provider account or workspace in one repository. See `Shared Environments` below.

Embedded projects
-----------------

Unfurl projects don't need to be live in a stand-alone git repository, they can be in any directory in a git repository. Unfurl commands will search for the nearest `unfurl.yaml` file or ``.unfurl`` directory.

This allows you to place an application's blueprint in an application's code repository. For example, if you already have a git repository containing your application's code, this command:

.. code-block:: shell

    unfurl init --existing --empty

will commit an Unfurl project at ``.unfurl`` (the default project name if not specified) in the git repository that the current directory is located in.

Managing Git Repositories
-------------------------

Unfurl can automatically commit any changes to the project to one or more git repositories.

:cli:`unfurl git-status<unfurl-git-status>` will show the location and git status of each repository the project manages.

:cli:`unfurl git<unfurl-git>` ``<git command line>`` will run the given git command on each repository the project manages, so you can run ``unfurl git push`` after you set up the remotes. Or you can use the ``--push`` option with Unfurl's :cli:`deploy<unfurl-deploy-commands>` commands to have Unfurl automatically push any committed after the job finishes.

When a git repository is cloned inside of a project, that repository's directory will be added to ``.git/info/exclude`` of the project's repository (instead of ``.gitignore`` because we don't want this exclusion committed).

Environments
============

**Environments** are used to create isolated contexts that deployment process runs in. For example, you might create different context for different projects or different deployment environments such as **production** or **staging**.

Ensembles are meant to be self-contained and independent of their environment with any environment-dependent values and settings placed in the Ensemble's environment.
Ensembles are reproducible and location-independent while Unfurl projects manage the environment and dependencies. 

It can also associate ensembles with a named environment (using the ``environment`` field in the ``ensembles`` section of `unfurl.yaml`).

Default environments
--------------------

When creating new project ``--use-environment`` will set the ``default_environment`` field in the project's `unfurl.yaml`, which is applied to any ensemble that doesn't have an environment set.

When creating a new ensemble --use-environment sets the ensemble's environment (in the project's unfurl.yaml's ``ensembles`` section).

Shared Environments
-------------------

Create an Unfurl project that will manage your deployment environments and record changes to your cloud accounts, for example:

.. code-block:: shell

    unfurl init --as-shared-environment aws-staging --skeleton aws

Then any ensemble that uses that environment across projects will be added to the ```aws-staging`` project.

For example:

.. code-block:: shell

    unfurl init --use-environment aws-staging my_app_project staging

This creates a new ensemble named "staging" in a project named "my_app_project" and sets to deploy into the environment you specified with ``--use-environment`` option 

Because ``aws-staging`` was created as a shared environment, the ensemble will be added to the "aws-staging" project's repository even though it is managed by "my_app_project".

The unfurl_home coordinates between projects so both projects need to use the same unfurl_home.

Inheritance and precedence
--------------------------

A Unfurl project can set environment defaults in the ``defaults`` section of ``environments``. 

An ensemble can also declare what properties and values it is expecting in its environment along with defaults values in the ``environment`` section of its `manifest<ensemble_yaml>`.

The following search order is applied when searching for settings and objects in the ensemble's environment:

1. The named environment in the current project.
2. The named environment in the environment's `default project<Shared Environments>` if set, or in the `ensemble repository`'s project if present.
3. The named environment in the home project.
4. defaults in the current project
5. defaults in the environment's `default project<Shared Environments>` if set, or in the `ensemble repository`'s project if present.
6. defaults in home projects
7. ``environment`` section in the ensemble's manifest

Environment Sections
--------------------

Environments can contain the following `sections<environment_schema>`:

Variables
+++++++++

Specifies the runtime's environment variables to set or copy from the current environment. See `Environment Variables` for syntax.

.. _environment_inputs:

Inputs
++++++

Overrides the :std:ref:`inputs` declared in an ensemble's ``spec``. See `Topology Inputs` for more information.

Locals
++++++

Locals are properties specific to the local environment (e.g. proxy settings) and are accessed through the `local` expression function.

Secrets
+++++++

Secrets are like locals except they are marked :std:ref:`sensitive` and redacted or encrypted when necessary. They are accessed through the `secret` expression function. See :std:ref:`Secrets` for more info.

Locals and secrets:

A map of names and values of locals or secrets with one reserved name:

:``schema``: a JSON schema ``properties`` object describing the schema for the map. If missing, validation of the attributes will be skipped.

Repositories
++++++++++++

You can specify repositories using TOSCA's `tosca_repositories` syntax in the environment so ensemble can reference a repository by name to specify its location.  Repositories can be aliased in the same manner as described in :std:ref:`connections`.

Imports
+++++++

You can include TOSCA's `tosca_imports` statements in the environment and those TOSCA templates will be imported into the ensemble's service template.

Connections
+++++++++++

A map of `connection templates<Connections>`. Connection templates are TOSCA relationship templates that represent connections to cloud providers and other online services.
The properties for each connection type match the environments variables commonly associated with each cloud provider. You can directly set the properties here or set the corresponding environments variables. If directly set here, their corresponding environments variable will be set when executing a job.

`Connection templates<Connections>` can be aliased by setting its value to the name of another connection template. If the name uses the form "<env_name>:<connection_name>" then it will be set to "connection_name" in the environment "env_name".  

When environments are merged, you can delete the inherited connection by setting its key to null in the overriding environment.

External
++++++++

This specifies instances and connections that will be imported from external ensembles. See `External ensembles`.

cloudmaps
+++++++++

You can configure the cloud map location and repository hosts used for synchronize in the environments section of `unfurl.yaml`. See `Configuration`.


lfs_lock
++++++++

See `locking`.

External ensembles
==================

Ensembles from external Unfurl projects can be imported into an Unfurl environment, allowing ensembles in that environment to access external resources.

The `external` section of an environment lets you declare instances that are imported from external manifests. Instances listed here can be accessed in two ways: One, they will be implicitly used if they match a node template that is declared abstract using the "select" directive (see "3.4.3 Directives"). Two, they can be explicitly referenced using the `external` expression function.

Resources can be explicitly imported (document external names!) or dynamically selected given a criteria using TOSCA's `"select" node template directive<tosca.NodeTemplateDirective.select>`.

There are 3 instances that are always implicitly imported even if they are not declared:

- The ``localhost`` instance that represents the machine Unfurl is currently executing on. This instance is accessed through the ``ORCHESTRATOR`` keyword in TOSCA and is defined in the home manifest that resides in your Unfurl home folder.

:manifest: A map specifying the location of the manifest. It must contain a ``file`` key with the path to the ensemble and optionally either a ``repository`` key indicating the name of the repository where the file is located or a ``project`` key to indicate the project the ensemble is in.
:instance: (default: "*") The name of the instance within the ensemble to make available.
  If ``*`` all instances in the ensemble will be available.

:uri: The ``uri`` of the ensemble. If it is set and it doesn't match the retrieved ensemble's URI a validation error will occur.

:``schema``: a JSON schema ``properties`` object describing the schema for the map. If missing, validation of the attributes will be skipped.

Creating projects
==================

To create your first Unfurl project run :cli:`unfurl init<unfurl-init>`

This will create a new project and commit it to new git repository unless the
``--existing`` flag is used. If its specified, Unfurl will search the current directory and its parents looking for the nearest existing git repository. It will then add the new project to that repository if one is found. (You can set the ``UNFURL_SEARCH_ROOT`` environment variable to set the directory where the search stops.)

:cli:`unfurl init<unfurl-init>` will also create an ensemble in the project (unless the ``--empty`` flag used).
By default, a separate, local git repository will be created for the ensemble. Use the ``--mono`` flag to add the ensemble to the project's git repository or use the ``--submodule`` flag to add the ensemble's git repository as a submodule of the project's git repository.

Keeping the ensemble repository separate from the project repository is useful
if the resources the ensemble creates are transitory or if you want to restrict access to them.
Using the ``--submodule`` option allows those repositories to be easily packaged and shared with the project repository but still maintain separate access control and git history for each ensemble.

Creating an ensemble in a new repository will add a ``vault_secrets`` secret with a generated password to ``local/unfurl.yaml`` and add a ``secrets/secrets.yaml`` file to the repository.  Or in any new project by setting the `VAULT_PASSWORD skeleton variable <vault_password_var>`.

.. important::

  Store the vault password found in ``ensemble/local/unfurl.yaml`` in a safe place! This password is used to encrypt any sensitive data committed to repository. See :doc:`secrets` for more information.

Project Skeletons
=================

New Unfurl projects and ensembles are created from a "project skeleton", which is a directory containing Jinja2 templates that are used to render the project files.

The :cli:`--skeleton<cmdoption-unfurl-init-skeleton>` option lets you specify an alternative to the default project skeleton. Unfurl includes several skeletons for the major cloud providers like AWS. You can see all the built-in project skeletons :unfurl_github_tree:`here <unfurl/skeletons>` or use an absolute path to specify your own. 

You can pass skeleton variables to the skeleton Jinj2a templates using the :cli:`--var<cmdoption-unfurl-init-var>` option, like the example `below<vault_password_var>`.


Cloning projects and ensembles
==============================

Use the :cli:`unfurl clone<unfurl-clone>` command to clone projects and ensembles. Its syntax is:

.. code-block:: shell

    unfurl clone [options] <source> [<dest>]

where:

``<source>`` can be a git URL or local file path.
Git URLs can specify a particular file in the repository using an URL fragment like ``#:<path/to/file>`` or ``#<branch_or_tag>:<path/to/file>``.
You can also use a cloudmap url like ``cloudmap:<package_id>``, which will resolve to a git URL.

``<dest>`` is a file path. If ``<dest>`` already exists and is not inside an Unfurl project, clone will exit in error. If omitted, the destination name is derived from the source and created in the current directory. 

Depending on the ``<source>``, use the clone command to accomplish one of the following:

Clone a project
---------------

If ``<source>`` points to a project, the project will be cloned.

If the source project is a blueprint project (i.e. it doesn't contain any ensembles) a new ensemble will also be created (`see below<Create a new ensemble from source>`) in the cloned project -- use the ``--empty`` option to skip creating the new ensemble.

The exception to this is when source is also a local file path and ``<dest>`` is an existing project, in that case the source project will just be registered with the destination project instead of cloned, and a new ensemble will be created (see below). Use an URL like ``file:path/to/project`` as the source to force cloning.

Clone an ensemble
-----------------

To clone a ensemble, set the ``<source>`` to the repository that the ensemble appears in. 

If ``<dest>`` is an existing project, the ensemble's project will be cloned inside the existing project and the effective environment of an ensemble will be the merger of the environment of the ensemble's project and the outer project's, as described in :ref:`Inheritance and precedence` above. 

If ``<dest>`` is empty the ensemble will be registered with the `unfurl home project<unfurl home>` (if present) and you can use the ``--use-environment`` option to specify which environment in the home project to to use.  

Either scenario allows you to put local specific settings in a local or home project without having to modify the shared ensemble.

.. tip::

  Shared ensembles should configure a `remote lock<Locking>` to prevent simultaneous deployments.

Create a new project from a CSAR archive
----------------------------------------

* If ``<source>`` is a TOSCA Cloud Service Archive (CSAR) (a file path with a .csar or .zip extension) and ``<dest>`` isn't inside an existing project, a new Unfurl project will be created with the contents of the CSAR copied to the root of the project. A new ensemble will also created unless the ``--empty`` flag is used.

Create a new ensemble from source
----------------------------------

Clone can create a new ensemble when:

* ``<source>`` explicitly points to an ensemble template or a TOSCA service template, a new ensemble is created from the template.
* ``<source>`` explicitly points to an ensemble. A new ensemble is created from that source (just the "spec" section is copied, not the status and it will have new `uri<uris>`).
* ``<source>`` is a blueprint project (ie. a project that contains no ensembles but does have an ``ensemble-template.yaml`` file) and the ``--empty`` flag wasn't used.
* ``<source>`` is a TOSCA Cloud Service Archive (CSAR) (a file path with a .csar or .zip extension) and the ``--empty`` flag wasn't used.

If ``dest`` is omitted or doesn't exist, the project that ``<source>`` is in will be cloned and the new ensemble created in the cloned project. If ``dest`` points to an existing project and ``<source>`` is a git url and not a local file path, the source repository will be cloned into the existing project.  If ``dest`` points to an existing project and ``<source>`` is a TOSCA Cloud Service Archive (CSAR) (a file path with a .csar or .zip extension), the contents of the CSAR will be copied to the ensemble's directory.

Notes
-----

* Clone shares many of the same command options as :cli:`unfurl init<unfurl-init>` as documented `above<Creating projects>`, such as ``--existing`` and ``--mono``.

* ``--design`` is like ``--empty`` except it also prepares the cloned project for developing the cloned blueprint, in particular, it prepares it for IDE editing (:cli:`unfurl validate<unfurl-validate>` will do this too).

* Unfurl uses git to clone the repositories, so if your git client has permission to access a git repository, Unfurl will have permission to clone it.

.. _vault_password_var:

* If a project has a file named ``.unfurl-local-template.yaml`` it will be used to create a new ``local/unfurl.yaml`` when it is cloned.  Projects that have vault-encrypted content store the vault password in that local file (if the default project skeleton was used), and the password can be set by including the VAULT_PASSWORD skeleton variable in the clone command, like:

    .. code-block:: shell

        unfurl clone --var VAULT_PASSWORD <password> ...

  The password needs to communicated out of band. Alternatively, you can set an environment variable of the form ``UNFURL_VAULT_<VAULTID>_PASSWORD`` at runtime.

* This step can be skipped if your project is hosted on `Unfurl Cloud`_, clone will retrieve the value password from `Unfurl Cloud`_. 

.. _publish_project:

Publishing your project
=======================

You can publish and share your projects like any git repository.
If you want to publish local git repositories on a git hosting service like github.com
(e.g. ones created by ``unfurl init`` or ``unfurl clone``) follow these steps:

1. Create corresponding empty remote git repositories.
2. Set the new repositories as the remote origins for your local repositories
   with this command:

   ``git remote set-url origin <remote-url>``

   Or, if the repository is a git submodule (see :cli:`--submodule<unfurl-init>`) use:

   ``git submodule set-url <submodule-path> <remote-url>``

3. Commit any needed changes in the repositories with ``unfurl commit``.  (Use ``unfurl git-status`` to see the uncommitted files across the project's repositories.)

4. Running ``unfurl git push`` will push all the repositories in the project.

Browser-based Admin User Interface
==================================

.. automodule:: unfurl.server.gui

See `Browser-based UI (experimental)` for more information.

Unfurl Home
===========

Unfurl Home is an Unfurl project that contains local settings and resources that are shared with other projects on that machine.

When Unfurl starts, it looks for the home project (by default in ``~/.unfurl_home``) and, if it exists, will merge its settings with the current project. You can control its location by using the ``--home`` global option or setting the environment variable ``UNFURL_HOME``. Setting either to an empty string disables loading the Unfurl home project.

When executing an Unfurl command, the loaded Unfurl Home will:

* Merge its `environments` with the current project's environments (See :ref:`Inheritance and precedence`)
* Register the project and its local repositories with Unfurl Home project so local projects and repositories can reference each other without having to clone the repository.
* If it contains an `execution runtime` (created by default), execute the Unfurl command line in it.
* If the command runs a job that requires local artifacts to be installed, they will deployed in the home project's ensemble.

Creating Unfurl Home
--------------------

The Unfurl home project is created automatically if it is missing when you run :cli:`unfurl init<unfurl-init>`.
It will be created using the :unfurl_github_tree:`home <unfurl/skeletons/home>` project skeleton and `execution runtime` is added to it. 

Alternatively, you can create the home project manually:

.. code-block:: shell

    unfurl home --init

This will create an Unfurl project located at ``~/.unfurl_home``, unless you specify otherwise using the ``--home`` global option. It will contain local configuration settings that will shared with your other projects and also creates an isolated environment to run Unfurl in.

By default it will create one git repository for the project and the ensemble -- you can override this using the ``--poly`` option.

Or, if you have an existing home project, you can just clone it like any other project.

To create a new `execution runtime` for the home project, use the ``runtime`` command, for example: ``unfurl runtime --init ~/.unfurl_home``.

As Unfurl Home is a standard Unfurl project, you can customize it and deploy it like any other project.
Resource deployed in your Unfurl project can be access by other projects by declaring the home project as an `external ensemble<external ensembles>`. See the :unfurl_github_tree:`home project skeleton<unfurl/skeletons/home>` for an example of how to configure this.

.. tip::

    The "connections" and "repositories" environment sections can reference templates in different environments defined in the home project.  For example, if you defined connection called "k8s" in the "home" environment defined in your home project, another project's environment can set its "primary_provider" connection to it like this:

    .. code-block:: yaml

      connections:
        primary_provider: home:k8s

Execution Runtime
=================

A Unfurl execution runtime is an isolated execution environment that Unfurl can run in. This can be a Python virtual environment or a Docker container. When you run an Unfurl cli command that executes a job (such as :cli:`unfurl deploy<unfurl-deploy>` or :cli:`unfurl plan<unfurl-plan>`) with a runtime defined then Unfurl will proxy that command to the runtime and execute it there.

The runtime is specified by the :option:`unfurl --runtime` CLI argument. If this is missing, it will look for a Python virtual environment directory (``.venv``) in the current project's directory and then in your `unfurl home`. By default, Unfurl will create a Python virtual environment in :ref:`~/unfurl_home<unfurl home>` when the home project is created. You can disable use of a runtime entirely using the :option:`unfurl --no-runtime` CLI argument.

The following runtime types can be specified as the argument to the ``--runtime`` option.

venv
------

The format for the ``venv:`` runtime specifier is one of:

``venv:[folder with a Pipfile]:[unfurl version]``

or

``venv:`` (use the default folder and default unfurl version)

If the Pipfile folder isn't specified the default one that ships with the Unfurl package will be used. In either case it will be copied to the root of the project the runtime is being installed in.
When the Python virtual environment is created it install the packages specified in the Pipfile (and Pipfile.lock if present).

Now you can use ``pipenv`` to install additional packages and commit the changes to ``Pipfile`` and ``Pipfile.lock`` to the project repository.

You can also specify the version of unfurl to use when the runtime is invoked.

The format for the unfurl version specifier is: ``[URL or path to an Unfurl git repository] ['@' [tag]]``

If ``@tag`` is omitted the tag for the current release will be used.
If ``@`` included without a tag the latest revision will be used
If no path or url is specified ``git+https://github.com/onecommons/unfurl.git`` will be used.

Some examples:

``@tag``

``./path/to/local/repo``

``./path/to/local/repo@tag``

``./path/to/local/repo@``

``git+https://example.com/forked/unfurl.git``

``@``

If omitted, the same version of Unfurl that is currently running will be used.
If specified, the package will be installed in "developer mode" (``-e``) by Pip.

.. tip::

  You can now upgrade Unfurl using pip normally from with in the virtual environment:

  ``source .venv/bin/activate; pip3 install -e --upgrade unfurl``


docker
------

The format for the ``docker:`` runtime specifier is:

``docker:[image]?:[tag]? [docker_args]?``

If ``image`` is omitted, "onecommons/unfurl" is used.
If ``tag`` is omitted, the image tag is set to the version of the Unfurl instance that is executing this command.

For example, if both omitted (e.g. ``docker:``) and you are running version 0.3.1 of Unfurl, the container image "onecommons/unfurl:0.3.1" will be used.

Anything thing after the tag will be treated as arguments to be passed to the docker run command that is called when executing this runtime.

.. tip::

  Since specifying ``docker_args`` will require a space separator, the whole runtime argument will have to be quoted.

shell default
-------------

If neither ``venv:`` or ``docker:`` is specified the ``--runtime`` option's argument is treated as a shell command with the unfurl command appended to it.
