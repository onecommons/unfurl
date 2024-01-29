====================
Expression Functions
====================

TOSCA functions and other stand-alone functions
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Stand-alone functions don't need to be wrapped in an "eval"

  ============================  ========================================================
  Key                           Value
  ============================  ========================================================
  `concat`                      ``[ string* ]``
  `get_artifact`                ``[ instance_name, artifact_name]``
  `get_attribute`               ``[ instance_name, req_or_cap_name?, property_name, index_or_key* ]``
  :std:ref:`get_env`            :regexp:`[ name, default? return ] | name`
  :std:ref:`get_input`          ``name``
  :std:ref:`get_nodes_of_type`  ``type_name``
  `get_property`                ``[ instance_name, req_or_cap_name?, property_name, index_or_key* ]``
  :std:ref:`has_env`            ``name``
  `join`                        ``[ string+, delimiter? ]``
  `q`                           ``any``
  `token`                       ``[ string, token, index]``
  ============================  ========================================================

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

  ===============================  ==================================================
  Key                              Value
  ===============================  ==================================================
  :std:ref:`abspath`               path | [path, location, mkdir?]
  `and`                            [test+]
  `eq`                             [a, b]
  external                         name
  `file`                           (see below)
  foreach                          {key?, value?}
  :std:ref:`get_dir`               location | [location, mkdir?]
  `is_function_defined`            function name
  `if`                             (see below)
  local                            name
  :std:ref:`lookup`                (see below)
  `or`                             [test+]
  `not`                            expr
  `python`                         path#function_name | module.function_name
  `secret`                         name
   :std:ref:`sensitive`            any
  :std:ref:`tempfile`              (see below)
  :std:ref:`template`              contents
  :std:ref:`to_dns_label`          string or map or lists
  :std:ref:`to_googlecloud_label`  string or map or list
  :std:ref:`to_kubernetes_label`   string or map or list
  :std:ref:`to_label`              string or map or list
  :std:ref:`urljoin`               [scheme, host, port?, path?, query?, fragment?]
  `validate`                       [contents, schema]
  ===============================  ==================================================

abspath
^^^^^^^

  :path: A file path
  :location: (optional) A named folder (see :std:ref:`get_dir`)
  :mkdir: (default: false) If true, create the folder if missing.

  Get the absolute path to the given path. If ``location`` is supplied it will be
  relative to that location (see :std:ref:`get_dir`) otherwise it will be relative to the current directory.

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
  encoding? "binary" | "vault" | "json" | "yaml" | "env" | python_text_encoding
  contents? any
  ========= ===============================

  ``encoding`` can be "binary", "vault", "json", "yaml", "env" or an encoding registered with the Python codec registry

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

is_function_defined
^^^^^^^^^^^^^^^^^^^

  :function: function name of a expression function

Evaluates to true if the given expression function is available. 
In the following example, the first expression returns true normally but false if a safe evaluation context.
The second expression always returns false.

.. code-block:: YAML

    eval:
      is_function_defined: get_env

    eval:
      is_function_defined: nope

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

to_dns_label
^^^^^^^^^^^^

Convert the given argument (see :std:ref:`to_label` for full description) to a DNS label (a label is the name separated by "." in a domain name).
The maximum length of each label is 63 characters and can include
alphanumeric characters and hyphens but a domain name must not commence or end with a hyphen.

Invalid characters are replaced with "--".

to_googlecloud_label
^^^^^^^^^^^^^^^^^^^^

Convert the given argument (see :std:ref:`to_label` for full description) to a kubernetes label 
following the rules found here https://cloud.google.com/resource-manager/docs/creating-managing-labels#requirements

Invalid characters are replaced with "__".

to_kubernetes_label
^^^^^^^^^^^^^^^^^^^

Convert the given argument (see :std:ref:`to_label` for full description) to a kubernetes label 
following the rules found here https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/#syntax-and-character-set

Invalid characters are replaced with "__".

to_label
^^^^^^^^

Convert a string to a label with the constraints specified as keyword parameters
defined in the table below. If given a dictionary, all keys and string values are converted.
If give a list, ``to_label`` is applied to each item and concatenated using ``sep``.

When given a list each item is truncated proportionally. The example below returns "longpr.name.suffi.RC"
("RC" is a digest of the original value, added when truncating to reduce the likelihood of duplicate name clashes.)

.. code-block:: YAML

  eval:
    to_label:
    - longprefix
    - name
    - suffix
    sep: .
    max: 20


This following example returns "X1_CONVERT". ``digestlen`` is set to 0 to skip appending a digest.

.. code-block:: YAML

  eval:
    to_label: "1 convert me"
    replace: _
    max: 10
    case: upper
    digestlen: 0

============= ==========================================================================================
Key           Value
============= ==========================================================================================
allowed       Allowed characters. Regex character ranges and character classes. Defaults to "\w" (equivalent to ``a-zA-Z0-9_``)
replace       String Invalidate. Defaults to "" (remove the characters).
start         Allowed characters for the first character. Regex character ranges and character classes. Defaults to "a-zA-Z"
start_prepend If the start character is invalid, prepend with this string (Default: "x")
end           Allowed trailing characters. Regex character ranges and character classes. Invalid characters are removed if set.
max           Maximum length of label (Default: 63 (the maximum for a DNS name))
case          Case for label, one of "lower", "upper", "any" (no conversion) (Default: "any")
sep           Separator to use when concatenating a list. (Default: "")
digest        If present, append a short digest of derived from concatenating the label with this digest. If omitted, a digest is only appended when the label is truncated. (Default: null)
digestlen     If a digest is needed, the length of the digest to include in the label. 0 to disable. Default: 3 or 2 if max < 32
============= ==========================================================================================

urljoin
^^^^^^^

Evaluate a list of url components to a relative or absolute URL, 
where the list is ``[scheme, host, port, path, query, fragment]``.

The list must have at least two items (``scheme`` and ``host``) present 
but if either or both are empty a relative or scheme-relative URL is generated.
If all items are empty, ``null`` is returned.
The ``path``, ``query``, and ``fragment`` items are url-escaped if present.
Default ports (80 and 443 for ``http`` and ``https`` URLs respectively) are omitted even if specified
-- the following examples both evaluate to "http://localhost/path?query#fragment":

.. code-block:: YAML

  eval:
    urljoin: [http, localhost, 80, path, query, fragment]

  eval:
    urljoin: [http, localhost, "", path, query, fragment]


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
.instances     child instances (via the ``HostedOn`` relationship)
.capabilities  list of capabilities
.requirements  list of requirements
.relationships relationships that target this capability
.targets       map with requirement names as keys and target instances as values
.sources       map with requirement names as keys and source instances as values
.hosted_on     Follow .targets, filtering by the ``HostedOn`` relationship
.configured_by Follow .sources, filtering by the ``Configures`` relationship
.descendants   (including self)
.all           dictionary of child resources with their names as keys
============== ========================================================
