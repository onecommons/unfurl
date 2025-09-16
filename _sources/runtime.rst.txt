===================
Runtime Environment
===================

The following sections describe the runtime environment when Unfurl is executing an operation in a job.

Job Variables
==============

When a `task <Tasks>` is being evaluated the following variables are available as an `expression <Eval Expressions>` variable or a jinja2 template variable.
Instances are represented as a dictionary containing its properties and attributes and its `special keys`.

  :implementation: The artifact used by the operation's implementation (or null if one wasn't set).
  :inputs: A dictionary containing the inputs declared on the task's operation.
  :arguments: A dictionary containing inputs and properties passed to task's `artifact`.
  :connections: See :std:ref:`Connections` below.
  :SELF: The current task's target instance.
  :Self: The `Python DSL` `object <tosca.ToscaType>` representing ``SELF`` if one was defined, or null.
  :HOST: The host of SELF.
  :ORCHESTRATOR: The instance Unfurl is running on (``localhost``)
  :OPERATION_HOST: Current task's ``operation_host``. If it is not declared, defaults to ``localhost``.
  :SOURCE: If the task's target instance is a relationship, its source instance
  :TARGET: If the task's target instance is a relationship, its target instance
  :task: A dictionary containing the current task's settings. Its keys include:

``task`` Keys
~~~~~~~~~~~~~

  :cwd: The current working directory the task process is executing in.
  :dryrun: (boolean) Whether the current job has the ``--dryrun`` flag set.
  :name: Name of the current task
  :operation: The name of the operation the task is running.
  :reason: `Reason` the current task was selected, e.g. "add", "update", "prune", "repair"
  :target: The name of the instance that the task is operating on.
  :timeout: The task's timeout value if set,
  :verbose: An integer indicating log verbosity level: 0 (INFO) (the default), -1 CRITICAL (set by ``--quiet``), >= 1 DEBUG (set by ``-v``, ``-vv``, or ``-vvv``)
  :workflow: The current workflow (e.g. ``deploy``, ``undeploy``, or ``discover``)

Connections
===========

In order to execute operations on a resource Unfurl often needs connection settings,
in particular credentials for authorization. These settings could be
passed as inputs to each operation that needs them but this can be repetitive and inflexible. Instead you can define `Relationship Templates` that are globally available to operations. Their properties can be accessed through ``connections`` job variable or automatically exposed as `environment variables` as described in that section.
They can also be accessed through the :std:ref:`find_connection` expression function or :py:func:`unfurl.tosca_plugins.expr.find_connection`.

You can define connections in the following ways:

* In the `relationship templates` section of your TOSCA service template.
* In the :std:ref:`connections` section of the ensemble's environment.
* On the source node template by defining a requirement with inline relationship with ``default_for`` set to "SELF".
* Imported from an external ensemble by setting the ``connections`` key in the `external ensembles` section. The imported connections are added to the main ensemble's default connections. This allows the main ensemble to connect to resources in the imported ensemble, For example, a Kubernetes cluster. Its value is a list of names of the relationship templates to import from the external ensemble. To import connections defined inline in a requirement on a node template use ``<instance_name>::<requirement_name>`` as the name. 
* Added by a operation at runtime via a `resultTemplate <resultTemplate>` or the `TaskView.update_instances()` method.

A relationship template is treated as a connection when it has the ``default_for`` key set. If this key is present, it indicates that the relationship template doesn't need to be explicitly referenced in the ``requirements`` section of a node template as regular relationship templates
must be, but instead is applied to any target node that matches the relationship type's specification.

To determine if a connection is available to a task, the ``default_for`` value is evaluated against the task's target node and capability and all of the target node's HOST ancestors. The ``default_for`` key can be set to one of the following values:

- "ANY"
- "SELF" (when set inline in a requirement)
- the name of a node template
- the name of a node type
- the name of a capability
- the name of a capability type

In additions to these global connections, if an :tosca_spec:`OPERATION_HOST<_Toc50125294>` or `the localhost ensemble` is defined, any explicit relationship templates defined between that node and the target node or the target node's HOST nodes will be added.

The connections available to a task are exposed though the ``connections`` job variable as a map where the keys are the name of the relationship template or the name of the type of the relationship.

For example, these expressions all evaluate to the same value:

  ``{{ "$connections::aws::AWS_ACCESS_KEY_ID" | eval }}``

  ``{{ "$connections::AWSAccount::AWS_ACCESS_KEY_ID" | eval }}``

  ``{{ "$connections::*::AWS_ACCESS_KEY_ID" | eval }}``

In addition, because the property ``AWS_ACCESS_KEY_ID`` is marked as an environment variable in the relationship's type definition, it will also be added to the environment when the operation executed.

Environment Variables
=====================

Each task has its own isolated set of environment variables that can be access via the :std:ref:`get_env` expression function or the `TaskView.environ` property.
Configurators that spawn processes like `shell` will use this to set the process's environment.

The environment is set as follows:

1. Copy the environment variables associated with the Unfurl process.
2. Apply the rules in the ``variables`` section in the ensemble's `environment`.
3. Add variables set by the connections that are available to this operation. Any property whose definition has ``env_var`` metadata set are added as environment variables. Unfurl's type definitions sets the common environment variables for cloud providers, for example ``KUBECONFIG`` or ``GOOGLE_APPLICATION_CREDENTIALS``.
4. Apply the rules declared in the ``environment`` section of the operation's `implementation` definition.
5. The operation can invoke the :std:ref:`to_env` expression function (or :py:func:`unfurl.tosca_plugins.expr.to_env`) to update environment variables.

When applying rules, Unfurl makes a copy of the current environment and applied each of its keys
in the order they are declared, adding the given key and value as
environment variables except keys starting with "+" and "-"
will copy or remove the variable from the current into environment
into the new one. In that case "*" and "?" are treated like filename wildcards and "!" negates the match:

.. code-block:: YAML

  name: value    # set name=value
  +name:         # copy name into the enviroment
  +name: default # copy value, set it to "default" if not present
  +prefix*:      # copy all variables matching "prefix*"
  +!prefix*:     # copy all except variables matching "prefix*"
  -!name:        # remove all except name
  -!prefix*:     # remove all except variables matching "prefix*"
  ^name: value   # treat name like a PATH and prepend value: e.g. /bin:$name

For example:

.. code-block:: YAML

  environment:
     -*:       # this will remove all environment variables
     +HOME:    # add HOME back
     FOO: bar  # set FOO = bar
     ^PATH: /bin # set PATH to /bin:$PATH

Values are converted to strings as follows:
If the value is null the rule is skipped.
If the value is a boolean, it is converted to "true" or "" if false.
Otherwise, it is converted to a string.
Patterns like ``${NAME}`` or ``${NAME:default value}`` in the value will be substituted with the value of the environment variable ``$NAME``. Use ``\${NAME}`` to ignore.

The following environment variables will always be copied from the parent environment unless explicitly removed or set:

.. documentedlist::
   :listobject: unfurl.util._sphinx_envvars
   :header: "Name"

If the ``ASDF_DATA_DIR`` environment variable is set or the ``https://github.com/asdf-vm/asdf.git`` repository is part of a current project
and a ``.tool-versions`` file exists (or ``$ASDF_DEFAULT_TOOL_VERSIONS_FILENAME``) in the root of a current project, then ``PATH`` environment variable will be configured to include the paths to the tools listed in that file.

Topology Inputs
===============

Topology :std:ref:`Inputs` are parameters passed to a service template when it is instantiated. They made available at runtime via the :ref:`get_input` expression function.

Inputs can come from any of the following sources, and are merged together:

* The `spec/inputs<ensemble_yaml>` section of the ensemble's manifest. For example:

  .. code-block:: yaml

    spec:
      inputs:
        foo: 0
      service_template:
        ...

* As a TOSCA extension, in the ``inputs_values`` section of the service template. For example:

  .. code-block:: yaml

    spec:
      service_template:
        inputs_values:
          foo: 0

* When creating or cloning an ensemble, the default `project skeleton<project skeletons>` will write inputs into this section using skeleton variables that start with ``input_``, for example, this command will render the yaml in the example above:

  .. code-block:: shell

      unfurl init --var input_foo 0

* The :std:ref:`Inputs section<environment_inputs>` of the current environment.

* From the command line:

You can add or override inputs when a job is run from the command line by passing job vars that start with ``input_``. For example, deploying with this command:

.. code-block:: shell

    unfurl deploy --var input_foo 1

will set ``foo`` to 1, overriding ``spec\inputs``.

Note that inputs passed via ``--var`` on the command line as parsed as YAML strings, as if they were embedded in the ensemble's YAML file.

Debugging Unfurl
================

The following environment variables can be set to enable debugging features and diagnostic output:

**UNFURL_DEBUGPY**
  Set to a port number (or "1" to use default port 5678) to wait for a remote debugger (like VS-Code) to attach on startup. Requires `debugpy <https://pypi.org/project/debugpy/>`_ to be installed.

**UNFURL_TEST_PRINT_YAML_SRC**
  Prints the YAML source generated when DSL Python is converted to TOSCA YAML. 

**UNFURL_TEST_PRINT_AST_SRC**
  When the TOSCA DSL imports loader executes Python code, convert the processed Python Abstract Syntax Tree (AST) back to Python source code and  print it. Helpful to debug exceptions raised from user-defined Python DSL templates, which might not have the source code reported in the exception stack trace.

**UNFURL_TEST_DUMP_SOLVER**
  Set to "1" or "2" to enable diagnostic output from the TOSCA solver when performs model inference.
  
  - "1": Prints nodes, requirements, requirement matches, property expressions, property matches, term matches, queries (final), and query results
  - "2": Additionally prints relationships, queries (intermediate), results (intermediate), and transitive matches

**UNFURL_TEST_SAFE_LOADER**
  When set to "never", disable the safe mode sandbox unconditionally. Any other non-empty value enables safe mode unconditionally.

**UNFURL_TEST_SKIP_LOADER**
  If set, disable the TOSCA DSL imports loader. The imports loader can sometimes confuse an interactive Python debugger, only use this in those scenarios.
