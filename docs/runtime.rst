.. _runtime:

===================
Runtime Environment
===================

Job Variables
==============

When a task is being evaluated the following variables are available as an `expression <Eval Expressions>` variable or a jinja2 template variable.
Instances are represented as a dictionary containing its properties and attributes and its `special keys`.

  :inputs: A dictionary containing the inputs passed to the task's operation.
  :connections: See `connections` below.
  :allConnections:  See `connections` below.
  :SELF: The current task's target instance.
  :HOST: The host of SELF.
  :ORCHESTRATOR: The instance Unfurl is running on (``localhost``)
  :ORCHESTRATOR_HOST: Current task's ``operation_host``. If it is not declared, defaults to ``localhost``.
  :SOURCE: If the task's target instance is a relationship, its source instance
  :TARGET: If the task's target instance is a relationship, its target instance
  :task: A dictionary containing the current task's settings. Its keys include:

``task`` Keys
~~~~~~~~~~~~~

  :name: Name of the current task
  :workflow: The current workflow (e.g. ``deploy``, ``undeploy``, or ``discover``)
  :target: The name of instance the task is operating on.
  :operation: The name of the operation the task is running.
  :dryrun: (boolean) Whether the current job has the ``--dryrun`` flag set.
  :reason: Reason the current task was selected, e.g. "add", "update", "prune", "repair"
  :cwd: The current working directory the task process is executing in.
  :verbose: An integer indicating log verbosity level: 0 (INFO) (the default), -1 CRITICAL (set by ``--quiet``), >= 1 DEBUG (set by ``-v``, ``-vv``, or ``-vvv``)
  :timeout: The task's timeout value if set,


Connections
===========

In order to execute operations on a resource Unfurl often needs connection settings,
in particular credentials for authorization. These settings could be
passed as inputs to each operation that needs them but this can be repetitive and inflexible.
TOSCA provides `relationship templates` which describe the properties of a relationship between
two nodes. Unfurl uses relationship templates as a mechanism to specify the connection settings
it need to connect to resources. It does so by adding a ``default_for`` key which indicates that the relationship template
doesn't need to be explicitly referenced in the ``requirements`` section of a node template as regular relationship templates
must be, but instead is applied to any target node that matches the relationship type's specification.

Because credentials likely are specific to the user or machine running Unfurl
you can define them with the ``localhost`` ensemble in :ref:`.unfurl_home<configure>` and by default they will be imported into the current ensemble. This can be explicitly specified when importing an external ensemble using the `connections` key as described in the `external ensembles` section.

As described in `Getting Started`, the ``localhost`` ensemble provides several connection relationship templates for connecting to the the most common cloud providers.

When Unfurl executes an operation it looks for relationship templates between the `OPERATION_HOST` and the node that the operation is targeting, including any connection relationship templates that apply. If those templates contain any environment variables they will be set otherwise they can be accessed through to variables:

:$allConnections: all connections available to the `OPERATION_HOST` as a dictionary where the key is the (mapped) name of the relationship template.
:$connections:  the current connections between the OPERATION_HOST and the target or the target's HOSTs as a list

For example, both these expressions evaluate to the same value:

  ``access_key: {{ "$allConnections::aws::AWS_ACCESS_KEY_ID" | eval }}``

  ``access_key: {{ "$connections::AWS_ACCESS_KEY_ID" | eval }}``

In addition, because environment value because that properties
 ``AWS_ACCESS_KEY_ID`` is marked as an environment variable in the relationship's type definition, it will also be added to the environment when the operation executed.

Environment Variables
=====================

You can control the environment variables are available while Unfurl is running
by the setting the `environment` directive.
When set in a `context` object it is applied globally or it can be appear in
the operation's `implementation` declaration where it will only be applied to that operation.

In either case, `environment` makes a copy of the current environment and applied each of its keys
in the order they are declared, adding the given key and value as
environment variables except keys starting with "+" and "-"
will copy or remove the variable from the current into environment
into the new one. In that case "*" and "?" are treated like filename wildcards and "!" negates the match:

.. code-block:: YAML

  name: value    # add name=value
  +name:         # copy name into the enviroment
  +name: default # copy value, set it to "default" if not present
  +!prefix*:     # copy all except variables matching "prefix*"
  -!name:        # remove all except name
  -!prefix*:     # remove all except variables matching "prefix*"
  ^name: /bin    # treat name like a PATH and prepend value: e.g. /bin:$name

For example:

.. code-block:: YAML

  environment:
     -*:       # this will remove all environment variables
     +HOME:    # add HOME back
     FOO: bar  # set FOO = bar

The following environment variables will always be copied from the parent environment unless explicitly removed or set:

.. documentedlist::
   :listobject: unfurl.util._sphinx_envvars
   :header: "Name"

If the ``ASDF_DATA_DIR`` environment variable is set or the ``https://github.com/asdf-vm/asdf.git`` repository is part of a current project
and a `.tool-versions` file exists (or ``$ASDF_DEFAULT_TOOL_VERSIONS_FILENAME``) in the root of a current project, then ``PATH`` environment variable will be configured to include the paths to the tools listed in that file.
