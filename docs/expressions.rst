================
Eval Expressions
================

.. contents::

When jobs are run Unfurl expressions that appear in the YAML configuration files are processed.

Expression Query Syntax
~~~~~~~~~~~~~~~~~~~~~~~

.. productionlist::
     expr    : segment? ("::" segment)*
     segment : [key] ("[" filter "]")* ["?"]
     key     : name | integer | var | "*"
     filter  : ['!'] [expr] [("!=" | "=") test]
     test    : var | ([^$[]:?])+
     var     : "$" name



Expression Function Syntax
~~~~~~~~~~~~~~~~~~~~~~~~~~

    ========  ==============  ============
    Key       Value           Description
    ========  ==============  ============
    eval      expr or func
    vars?     map
    select?   expr
    foreach?  {key?, value?}
    trace?    integer
    ========  ==============  ============

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

  Read or write a file
  ``encoding`` can be "binary", "vault", "json", "yaml" or an encoding registered with the Python codec registry

  .. code-block:: YAML

       eval:
         file:
         contents:
       select: contents

  ========= ===============================
  Key       Value
  ========= ===============================
  file:     path
  encoding? "binary" | "vault" | "json" | "yaml" | python_text_encoding
  contents? any
  ========= ===============================

  ``encoding`` can be "binary", "vault", "json", "yaml" or an encoding registered with the Python codec registry

  Key can be one of:

  path # absolute path
  contents # file contents (None if it doesn't exist)
  encoding

foreach
^^^^^^^

get_dir
^^^^^^^

  :location: a named folder
  :mkdir: (default: false) If true, create the folder if missing.

  Return an absolute path to the given named folder where ``name`` is one of:

  :.:   directory that contains the current instance's the ensemble
  :src: directory of the source file this expression appears in
  :home: The "home" directory for the current instance (committed to repository)
  :local: The "local" directory for the current instance (excluded from repository)
  :tmp:   A temporary directory (removed after unfurl exits)
  :spec.src: The directory of the source file the current instance's template appears in.
  :spec.home: The "home" directory of the source file the current instance's template.
  :spec.local: The "local" directory of the source file the current instance's template.
  :project: The root directory of the current project.
  :unfurl.home: The location of home project (``UNFURL_HOME``).

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

  Evaluate contents as an Ansible-flavored Jinja2 template

validate
^^^^^^^^

  Return true if the first argument conforms to the JSON schema supplied as the second argument.

Semantics
~~~~~~~~~

Each segment specifies a key in a resource or JSON/YAML object.
"::" is used as the segment delimitated to allow for keys that contain "." and "/"

Path expressions evaluations always start with a list of one or more Resources.
and each segment selects the value associated with that key.
If segment has one or more filters
each filter is applied to that value -- each is treated as a predicate
that decides whether value is included or not in the results.
If the filter doesn't include a test the filter tests the existence or non-existence of the expression,
depending on whether the expression is prefixed with a "!".
If the filter includes a test the left side of the test needs to match the right side.
If the right side is not a variable, that string will be coerced to left side's type before comparing it.
If the left-side expression is omitted, the value of the segment's key is used and if that is missing, the current value is used.

If the current value is a list and the key looks like an integer
it will be treated like a zero-based index into the list.
Otherwise the segment is evaluated again all values in the list and resulting value is a list.
If the current value is a dictionary and the key is "*", all values will be selected.

If a segment ends in "?", it will only include the first match.
In other words, "a?::b::c" is a shorthand for "a[b::c]::0::b::c".
This is useful to guarantee the result of evaluating expression is always a single result.

The first segment:
If the first segment is a variable reference the current value is set to that variable's value.
If the key in the first segment is empty (e.g. the expression starts with '::') the current value will be set to the evaluation of '.all'.
If the key in the first segment starts with '.' it is evaluated against the initial "current resource".
Otherwise, the current value is set to the evaluation of ".ancestors?". In other words,
the expression will be the result of evaluating it against the first ancestor of the current resource that it matches.

If key or test needs to be a non-string type or contains a unallowed character use a var reference instead.

When multiple steps resolve to lists the resultant lists are flattened.
However if the final set of matches contain values that are lists those values are not flattened.

For example, given:

.. code-block:: javascript

 {x: [ {
         a: [{c:1}, {c:2}]
       },
       {
         a: [{c:3}, {c:4}]
       }
     ]
 }

``x:a:c`` resolves to:
 ``[1,2,3,4]``
not
 ``[[1,2], [3,4]])``

(Justification: It is inconvenient and fragile to tie data structures to the particular form of a query.
If you want preserve structure (e.g. to know which values are part
of which parent value or resource) use a less deep path and iterate over results.)


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
.descendents   (including self)
.all           dictionary of child resources with their names as keys
============== ========================================================
