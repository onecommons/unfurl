====================
Expression Functions
====================

TOSCA functions and other stand-alone functions
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Stand-alone functions don't need to be wrapped in an "eval"

  =================== ========================================================
  Key                 Value
  =================== ========================================================
  `concat`            ``[ string* ]``
  `get_artifact`      ``[ instance_name, artifact_name]``
  `get_attribute`     ``[ instance_name, req_or_cap_name?, property_name, index_or_key* ]``
  `get_env`           :regexp:`[ name, default? return ] | name`
  `get_input`         ``name``
  `get_nodes_of_type` ``type_name``
  `get_property`      ``[ instance_name, req_or_cap_name?, property_name, index_or_key* ]``
  `has_env`           ``name``
  `join`              ``[ string+, delimiter? ]``
  `q`                 ``any``
  `token`             ``[ string, token, index]``
  =================== ========================================================

concat
^^^^^^

  The concat function is used to concatenate two or more string values. See :tosca_spec:`concat <_Toc454458585>`.
  ``concat`` can optionally take a ``sep`` keyword argument the specifies as a string the separator to use when joining the list.

get_artifact
^^^^^^^^^^^^

  The get_artifact function is used to retrieve artifact location between modelable entities defined in the same service template.

  If the artifact is a Docker image, return the image name in the form of
  "registry/repository/name:tag" or "registry/repository/name@sha256:digest"

  If entity_name or artifact_name is not found return ``null``.

  See :tosca_spec:`get_artifact <_Toc50125538>`.

get_attribute
^^^^^^^^^^^^^

  The get_attribute function is used to retrieve the values of named attributes declared by the referenced node or relationship template name.
  See :tosca_spec:`TOSCA Attribute Functions <_Toc50125522>`.

get_env
^^^^^^^

  Returns the value of the given environment variable name.
  If NAME is not present in the environment, return the given default value if supplied or return None.

  e.g. {get_env: NAME} or {get_env: [NAME, default]}

  If the value of its argument is empty (e.g. [] or null), return the entire dictionary.

.. _get_input:

get_input
^^^^^^^^^

  The get_input function is used to retrieve the values of properties declared within the inputs section of a TOSCA Service Template.
  See :tosca_spec:`TOSCA Property Functions <_Toc50125513>`

get_nodes_of_type
^^^^^^^^^^^^^^^^^

  The get_nodes_of_type function can be used to retrieve a list of all known instances of nodes of the declared Node Type.

get_property
^^^^^^^^^^^^

  The get_property function is used to retrieve property values between modelable entities defined in the same service template.
  See :tosca_spec:`TOSCA Property Functions <_Toc26969456>`

has_env
^^^^^^^

  The ``has_env`` function returns a boolean indicating whether the given variable is found in the current environment.

join
^^^^

  The join function is used to join an array of strings into a single string with optional delimiter. See

q
^

  Quote the given value without evaluating it.
  For example:

  .. code-block:: YAML

      q:
        eval:
           this will not be evaluated

  Will evaluate to:

  .. code-block:: YAML

    eval:
       this will not be evaluated

  without any further evaluation.

token
^^^^^

  The token function is used on a string to parse out (tokenize) substrings separated by one or more token characters within a larger string.

Expression Functions
~~~~~~~~~~~~~~~~~~~~

  ===================== ================================
  Key                   Value
  ===================== ================================
  `abspath`             path | [path, location, mkdir?]
  `and`                 [test+]
  `eq`                  [a, b]
  external              name
  `file`                (see below)
  foreach               {key?, value?}
  `get_dir`             location | [location, mkdir?]
  `if`                  (see below)
  local                 name
  `lookup`              (see below)
  `or`                  [test+]
  `not`                 expr
  `python`              path#function_name | module.function_name
  `secret`              name
   :std:ref:`sensitive` any
  `tempfile`            (see below)
  `template`            contents
  `validate`            [contents, schema]
  ===================== ================================

abspath
^^^^^^^

  :path: A file path
  :location: (optional) A named folder (see `get_dir`)
  :mkdir: (default: false) If true, create the folder if missing.

  Get the absolute path to the given path. If ``location`` is supplied it will be
  relative to that location (see `get_dir`) otherwise it will be relative to the current directory.

  Also available as a jinja2 filter.

and
^^^

  Evaluates each expression in the list until an expression evaluates as false and
  returns the result of the last expression evaluated.

eq
^^

external
^^^^^^^^

  Return an instance

file
^^^^

  Read or write a file. If the ``contents`` keyword argument is present, a file will be written
  upon evaluation of this function, otherwise it will be read.

  .. code-block:: YAML

      # read
      eval:
        file: foo/local.config
      select: contents

      # write
      eval:
        file: path.txt.vault
        contents: "this will be saved as a vault encrypted file"
        encoding: vault
      select: path

  ========= ===============================
  Key       Value
  ========= ===============================
  file:     path
  dir?:     path
  encoding? "binary" | "vault" | "json" | "yaml" | python_text_encoding
  contents? any
  ========= ===============================

  ``encoding`` can be "binary", "vault", "json", "yaml" or an encoding registered with the Python codec registry

  The ``select`` clause can evaluate the following keys:

  ========= ===============================
  Key       Returns
  ========= ===============================
  path:     absolute path of the file
  encoding  encoding of the file
  contents? file contents (None if it doesn't exist)
  ========= ===============================

foreach
^^^^^^^

get_dir
^^^^^^^

  :location: a named folder
  :mkdir: (default: false) If true, create the folder if missing.

  Return an absolute path to the given named folder where ``name`` is one of:

  :.:   Directory that contains the current instance's ensemble
  :src: Directory of the source file this expression appears in
  :artifacts: Directory for the current instance (committed to repository).
  :local: The "local" directory for the current instance (excluded from repository)
  :secrets: The "secrets" directory for the current instance (files written there are vault encrypted when committed to the repository)
  :tmp:   A temporary directory for the instance (removed after unfurl exits)
  :tasks: Job specific directory for the current instance (excluded from repository).
  :operation: Operation specific directory for the current instance (excluded from repository).
  :workflow: Workflow specific directory for the current instance (excluded from repository).
  :spec.src: The directory of the source file the current instance's template appears in.
  :spec.home: Directory unique to current instance's TOSCA template (committed to the spec repository).
  :spec.local: Local directory unique to current instance's TOSCA template (excluded from repository).
  :project: The root directory of the current project.
  :unfurl.home: The location of home project (UNFURL_HOME).

  Otherwise look for a repository with the given name and return its path or None if not found.

  Also available as a jinja2 filter.

if
^^

  ======== ===============================
  Key      Value
  ======== ===============================
  if       mapped_value
  then?    expr
  else?    expr
  ======== ===============================

  Example: this will always evaluate to "expected":

  .. code-block:: YAML

    eval:
      if:
        or:
          - not: $a
          - $a
      then: expected
      else: unexpected
    vars:
      a: true

lookup
^^^^^^

  ========= ===============================
  Key       Value
  ========= ===============================
  lookup    {name: args,
            kwargs*: value}
  ========= ===============================

  .. code-block:: YAML

      eval:
        lookup:
          env: TEST_ENV

      eval:
        lookup:
          env: [TEST_ENV, default]

      eval:
        lookup:
          url: https://example.com/foo.txt
          validate_certs: true

local
^^^^^

or
^^

  Evaluates each item until an item evaluates as true, returns that value or false.

not
^^^

  Evaluates the item and returns its negation.

python
^^^^^^

  ======== =========================================
  Key      Value
  ======== =========================================
  python   path#function_name | module.function_name
  args?    mapped_value
  ======== =========================================

  .. code-block:: YAML

    eval:
      python: path/to/src.py#func

    # or:

    eval:
      python: python_module.func

    # with args:

    eval:
      python: python_module.func
      args:   foo

  Execute the given python function and evaluate to its return value.
  If the path to the python script is a relative path, it will be treated as relative to the current source file
  (ie. the template file that is invoking the expression).
  The function will being invoke the current `RefContext` as the first argument.
  If ``args`` is declared, its value will passed as a second argument to the function.

secret
^^^^^^

  Return the value of the given secret. It will be marked as sensitive.

sensitive
^^^^^^^^^

  Mark the given value as sensitive.

tempfile
^^^^^^^^

  Create local, temporary file with the specified content.
  It will be deleted after ``unfurl`` process exits.

  .. code-block:: YAML

    eval:
      tempfile: "contents"
      encoding: vault
      suffix: .json

  ========= ===============================
  Key       Value
  ========= ===============================
  tempfile  contents
  encoding? "binary" | "vault" | "json" | "yaml" | python_text_encoding
  suffix?
  ========= ===============================

  If ``encoding`` isn't specified, the file extension specified by ``suffix`` is used;
  if neither is specified, the encoding will be determined by the content, either utf8 text, binary or json or a 0 byte file if the content is null.

template
^^^^^^^^

Evaluate file or inline contents as an Ansible-flavored Jinja2 template.

.. code-block:: YAML

  eval:
    template:
      path: path/to/template.j2

.. code-block:: YAML

  eval:
    template: >
      {%if testVar %}success{%else%}failed{%endif%}
  vars:
    testVar: true

validate
^^^^^^^^

  Return true if the first argument conforms to the JSON schema supplied as the second argument.

Special keys
~~~~~~~~~~~~~
Built-in keys start with a leading **.**:

============== ========================================================
**.**          self
**..**         parent
.name          name of this instance
.type          name of instance's TOSCA type
.tosca_id      unique id of this instance
.tosca_name    name of the instance's TOSCA template
.status        the instance's :class:`unfurl.support.Status`
.state         the instance's :class:`unfurl.support.NodeState`
.parents       list of parents
.ancestors     self and parents
.root          root ancestor
.instances     child instances (via the ``hostedOn`` relationship)
.capabilities  list of capabilities
.requirements  list of requirements
.relationships relationships that target this capability
.targets       map with requirement names as keys and target instances as values
.sources       map with requirement names as keys and source instances as values
.descendents   (including self)
.all           dictionary of child resources with their names as keys
============== ========================================================
