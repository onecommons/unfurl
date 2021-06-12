===============
Unfurl Projects
===============

Ensembles are meant to be self-contained and independent of their environment with any
environment-dependent values and settings placed in the Ensemble's context.

Unfurl projects define these contexts and assign them to the project's ensembles.

Contexts
========

Contexts can contain:

Runtime
-------

An isolated execution environment to run the ensemble's job in. This can be directory to a Python virtual environment or a Docker container. If this is missing, it will look a Python virtual environment directory (``.venv``) in the project's directory. By default, Unfurl will create a Python virtual environment in :ref:`~/unfurl_home<configure>` when the home project is created.


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


Environment
-----------

Specifies the runtimes environment variables to set or copy from the current environment (see `Environment`)

Locals
------

Locals are properties specific to the local environment (e.g. proxy settings) and are accessed through the `local` expression function.

Secrets
-------

Secrets are like locals except they are marked :std:ref:`sensitive` and redacted or encrypted when necessary. They are accessed through the `secret` expression function. See :std:ref:`Secrets` for more info.

Connections
-----------

A map of connection templates containing account credentials to use. Inherited connection templates
can be renamed locally by including an item with the new name as its key and old name as its the value.

External
--------

This specifies instances and connections that will be imported from external ensembles. See `External ensembles`.

Inheritance and precedence
--------------------------

A Unfurl project can set context defaults. It can also declare named contexts and associate ensembles with a named context.

An Ensemble can also declare what properties and values it is expecting in its context along with defaults.

The following search order is applied when looking context up settings and objects:

1. named context in current project
2. named context in home project
3. defaults in current project
4. defaults in home projects
5. context section in the ensemble manifest

External ensembles
==================

The `external` section of the manifest lets you declare instances that are imported from external manifests. Instances listed here can be accessed in two ways: One, they will be implicitly used if they match a node template that is declared abstract using the "select" directive (see "3.4.3 Directives"). Two, they can be explicitly referenced using the `external` expression function.

There are 3 instances that are always implicitly imported even if they are not declared:

- The `localhost` instance that represents the machine Unfurl is currently executing on. This instance is accessed through the `ORCHESTRATOR` keyword in TOSCA and is defined in the home manifest that resides in your Unfurl home folder.

:manifest: A map specifying the location of the manifest. It must contain a ``file`` key with the path to the ensemble and optionally either a ``repository`` key indicating the name of the repository where the file is located or a ``project`` key to indicate the project the ensemble is in.
:instance: (default: "*") The name of the instance within the ensemble to make available.
  If ``*`` all instances in the ensemble will be available.
:uri: The ``uri`` of the ensemble. If it is set and it doesn't match the retrieved ensemble's URI a validation error will occur.

Locals and secrets:

:``attributes``: a map of the providing the names and values of locals or secrets
:``schema``: a JSON schema properties object describing the schema for each attribute. If missing, validation of the attributes will be skipped.

Project defaults
================

After running "init" your Unfurl project will look like:

ensemble/ensemble.yaml
ensemble-template.yaml
unfurl.yaml
local/unfurl.yaml

If the --existing option is used, the project will be added to the nearest repository found in a parent folder.
If the --mono option is used, the ensemble add the project repo instead of it's own.

Each repository created will also have .gitignore and .gitattributes added.

When a repository is added as child of another repo, that folder will be added to .git/info/exclude
(instead of .gitignore because they shouldn't be committed into the repository).

Include directives, imports, and external file reference are guaranteed to be local to the project.
Paths outside the project need to be referenced with a named repository.
Paths are always relative but you can optionally specify which repository a path is relative to.

There are three predefined repositories:

"self", which represents the location the ensemble lives in -- it will be
a "git-local:" URL or a "file:" URL if the ensemble is not part of a git repository.

"unfurl" which points to the Python package of the unfurl process -- this can be used to load configurators and templates
that ship with Unfurl.

"spec" which, unless explicitly declared, defaults to the project root or the ensemble itself if it is not part of a project.

Runtimes
========

TODO
