====================
Template Processing
====================

.. contents::
  :local:

Unfurl provides a few mechanisms for embedding logic into TOSCA service templates and Unfurl configuration files:

At parse time, YAML configuration files can contain `YAML Merge directives` that preprocess the YAML.

At runtime (e.g. when a job is running), TOSCA properties and inputs can contain `Eval Expressions` and `Jinj2a templates <Ansible Jinja2 Templates>` that are processed when those properties or inputs are accessed.

.. _yaml_merge_directives:

YAML Merge directives
=====================

When Unfurl's YAML parser encounters a YAML map key that matches the following pattern:

``'+'['?']['include'][*anchor][relative path][absolute path]``

and it treats it as a merge directive that updates the map by processing the directive and merging in its resolved value.

Merge keys can have the following components:

.. productionlist::
     merge key      : merge_directive | merge_anchor | merge_strategy
     merge_directive : "+"["?"][include][alias][relative_start][path]
     include        : "include"[ALPHNUM]*
     alias          : "*"[PCHAR]*[PCHAR except "."]
     relative_start : "."+
     path           : ["/" PCHAR*]+
     ALPHNUM        : a-z, A-Z, 0-9, _, -
     PCHAR          : any printable character except "/"
     merge_anchor   : "+&"
     merge_strategy : "+%"


Each of these components are optional but at least one needs to be present, otherwise the key is ignored and included in the final document.

A leading '?' indicates that reference maybe missing, otherwise the processing will abort with an error.

``include`` indicates that value of the merge key includes a yaml or json file to load. Can have optional trailing alphanumeric characters to ensure key is unique.

``*alias``: a reference to merge anchor that appears in either the current document or, a file was specified, from that file.

``relative start``: '.'+ The path is relative to the current location this key appears in.

``path``: [/path]+ A path that is resolved following <jsonpointer> RFC

If the directive contains "include", its value can be a map or a string. If it is a string, treat the value as a file path or a URL to a YAML or JSON file.
If it is a map, it must have a ``file`` key whose value is file path or URL, and optionally a "repository" key and a "merge" key whose value is a merge directive value.

For other directives, if the value of the merge directive is empty, merge the result using algorithm described below.
If the value contains "raw", include the result without any further processing.
If the value contains "overlay", set the merge algorithm to "overlay" mode as described below.

The resolved value is merged into the map containing the directive using the following rules\:

  If the result of the lookup is not a JSON object or YAML map\:

    If result of the lookup is an array  and the map being replaced appears as an item in a array\:
      The array is spliced in place. (If you don't want that behavior just wrap the include in another array, e.g ``[{+/list1: null}]``)

    Otherwise, if the map being replaced has no other keys\:
      Replace the map by the result

    Otherwise, abort processing with a merge error

  Otherwise, recursively merge the maps\:
    for each key in the result object
        if the key doesn't exist in the target\:
          Add the key and value
        otherwise if the result value is not an object or array\:
          Ignore this key or, if in "overlay" mode, replace the target value with the result value
        otherwise, if value of the target key:
            is null\:
              Treat it like an empty object
            is an object with a merge directive with the value "whiteout" (``{+%: whiteout}``)\:
               Omit the target key from the result object
            is an object with a merge directive with the value "nullout" (``{+%: nullout}``)\:
              Set the target key to null in the result object
            is an object with a merge directive with the value "error" (``{+%: error}``)\:
              Abort merge and raise an error
            is a different type then the result value\:
              Ignore this key or, if in "overlay" mode, replace the target value with the result value.
            is an array \:
              For each item in the result array \:
                 If it is a object and \:
                    it contains a merge directive with the value "merge" (``+%: merge``)\:
                      if the position of the item is beyond the end of the target array\:
                         append the item
                      Otherwise, get the target array item at the same position\:
                        if it is an object\:
                           merge the items

                        Otherwise, replace target item with the result item.
                    or it contains a single key and there is object in the target array with the same key\:
                      merge the object into the target array item following the rules above

                 Otherwise if the item is not found in the target array, append the item to the target array.

        Otherwise, merge the result object value and target object value following these rules.



Restoring merge directives
--------------------------
When saving YAML config file that contained merge directive will attempt restore them even if the configuration has changed -- if the target object has changed, a new diff will be generated to reflect those changes.

Ansible Jinja2 Templates
========================

Unfurl will process any `Ansible-flavored Jinja2 templates <https://docs.ansible.com/ansible/latest/user_guide/playbooks_filters.html>`_ it encounters in strings while processing an Ensemble template.
You can also convert Python f-strings with type-safe expressions to Jinaj2 templates using the `jinja_template` decorator.

Unfurl's Jinja2 template rendering supports the full suite of filters and lookup plugins provided by Ansible as well as the following predefined variables, filters, and lookup plugins:

Filters
-------

  :eval: Evaluates the given `expression <eval expressions>` or function. Equivalent to `resolve_one`
         For example: ``{{ "::instance1::anAttribute" | eval }}``
  :map_value: Resolves any `eval expressions` or template strings embedded in the given map or list. Equivalent to :py:func:`unfurl.eval.map_value`.
  :which: Returns the full path to the given executable, like the ``which`` shell command.

  In addition, the following eval expression functions can be used as Jinja2 filters:
  :std:ref:`abspath`, :std:ref:`get_dir`, :std:ref:`inert`,
  :std:ref:`scalar`, :std:ref:`sensitive`,
  :std:ref:`to_label`,
  :std:ref:`to_dns_label`,
  :std:ref:`to_googlecloud_label`
  :std:ref:`to_kubernetes_label`.

Lookup plugins
--------------

  :unfurl: Evaluates the given `expression <eval expressions>`
           For example: ``{{ lookup("unfurl", "::instance1::anAttribute") }}``


Variables
---------

  :__unfurl: The current `RefContext`. This can be used to call `expression functions` as Jinja2 functions,
             for example: ``{{ __unfurl.to_label('a','b', sep='.') }}``
  :__now: The current time in seconds since the epoch (1970) (Python's ``time.time()``)
  :__python_executable: The location of the current python executable (Python's ``sys.executable``)


Eval Expressions
================

The basic form is a YAML map with at least a key named ``eval``:

.. code-block:: YAML

  eval: <expression> or <eval function> or <jinja2 expression>
  <optional eval keys>

Where ``<expression>`` is string matching the query syntax below, ``<function>`` is a map that is an `expression function <Expression Functions>`, or a ``<jinaj2 expression>``, as described above.

Eval Keys
----------

   =========  ==============  ==========================================
   Key        Value           Description
   =========  ==============  ==========================================
   eval       expr or func    the eval or jinja2 expression to evaluate
   vars?      map             define variables for the expression
   select?    expr            apply expression to the result
   foreach?   see `foreach`   apply expression to each item in result
   trace?     integer         enable detailed logging of evaluation
   strict?    bool or string  overrides current strict evaluation mode
   base_dir?  string          override base directory for relative paths
   =========  ==============  ==========================================

Expression Query Syntax
--------------------------

.. productionlist::
    expr    : segment? ("::" segment)*
    segment : [key] ("[" filter "]")* ["?"]
    key     : name | integer | var | "*"
    filter  : ['!'] [expr] [("!=" | "=" | "~=") test]
    test    : var | ([^$[]:?])+
    var     : "$" name

Evaluation Semantics
--------------------

Eval expressions are path-based queries where each segment selects values from the previous result. Eval expressions operate on `instances <instance_def>` and regular JSON/YAML objects and arrays. For example:

.. code-block:: YAML

  # start with my_instance
  eval: ::my_instance::my_property::key_in_prop

  # select the property "my_property" on the current instance  
  eval: .::my_property

  # my_property on nearest ancestor
  eval: my_property::key_in_prop

The first segment determines the initial value to start evaluation with. The examples above illustrate each have a first segment that is:

  * Empty (i.e. the expression starts with ``::``): the initial value will be set to the evaluation of `.all <Special keys>`, so the next segment will match against a map of all the instances in the current topology.

  * ``.`` The initial value set to the "current resource" -- basically, the instance that the expression is declared in.
  
  * Any other key (e.g "my_property" in the last example), sets the current value is set to the evaluation of `.ancestors <Special keys>`. In other words, if the current resource doesn't have that key, search for the nearest ancestor that does.

A ensemble can contain more than one topology when `substitution mappings <substitution_mappings>` are used. Instances in nested topologies can be referenced using ``substituted_node:inner_instance`` syntax. An instance name can start with ":" to anchor the name in the root topology.  Thus, ``:::my_instance`` always evaluates to the instance named "my_instance" in the root topology (equivalent to ``.apex::my_instance``).

The initial segment can also be a variable reference, where the current value is set to that variable's value, for example:

.. code-block:: YAML

  eval: $foo::my_key
  vars:
    foo:
      my_key: 1


Filters
~~~~~~~

If segment can have one or more filters.
Each filter is applied to that value -- each is treated as a predicate
that decides whether value is included or not in the results.
If the filter doesn't include a test, the filter tests the existence or non-existence of the expression,
depending on whether the expression is prefixed with a ``!``.

**Example:** Given a resource with attributes:

.. code-block:: javascript

  {
    d: {a: "va", b: "vb"},
    x: {
      a: [{c: 1}, {c: 2}, {b: "exists"}]
    }
  }

.. code-block:: YAML

  eval: d[a=va]
  # Result: {a: "va", b: "vb"}

  eval: d[a=va]::b
  # Result: "vb"

  eval: x::a[b]::c
  # Result: [1, 2]

  eval: x::a[b!=exists]::c
  # Result: []

  eval: x::a[!b]::c
  # Result: []

If the filter includes a test the left side of the test needs to match the right side.
If the right side is not a variable, that string will be coerced to left side's type before comparing it.
If the left-side expression is omitted, the value of the segment's key is used and if that is missing, the current value is used.

**Special keys**

Keys that start with "." are reserved and special meaning -- see this list of built-in `Special keys`.

Lists
~~~~~

Use number as the segment key to select items in a list or array. For example:

.. code-block:: javascript

  {
    b: [1, 2, 3],
    x: {
      a: [{c: 1}, {c: 2}, {c: 3}, {c: 4}]
    }
  }

.. code-block:: YAML

  eval: b::0
  # Result: 1

  eval: b::1
  # Result: 2

  eval: x::a::c
  # Result: [1, 2, 3, 4]

Use ``~=`` to test if a value is in the list. For example:

.. code-block:: YAML

  eval: b[~=2]
  # Result: [1, 2, 3]

  eval: b[~=4]
  # Result: []

``*`` Wildcard Operator
~~~~~~~~~~~~~~~~~~~~~~~

Use ``*`` to match all values. So to filter on the values in a map, select them all first before applying the filter, for example: ``.artifacts::*::[type=MyArtifactType]``.

**Example with wildcards:**

.. code-block:: javascript

  {
    d: {a: "va", b: "vb"},
    e: {
        a1: {b1: "v1"},
        a2: {b2: "v2"}
    }
  }

.. code-block:: YAML

  eval: d::*
  # Result: ["va", "vb"]

  eval: e::*::b2
  # Result: ["v2"]


``?`` Match Once Operator
~~~~~~~~~~~~~~~~~~~~~~~~~

If a segment ends in ``?``, it will only include the first match.
In other words, ``a?::b::c`` is a shorthand for ``a[b::c]::0::b::c``.
This is useful to guarantee the result of evaluating expression is always a single result.

**Example with** ``?`` **operator:**

.. code-block:: javascript

  {
    b: [1, 2, 3],
    x: [
      {a: [{c: 1}, {c: 2}]},
      {a: [{c: 3}, {c: 4}]}
    ]
  }

.. code-block:: YAML

  eval: x::a::c
  # Result: [1, 2, 3, 4]

  eval: x::a?::c
  # Result: [1, 2] (only first match of 'a')

  eval: x?::a[c=4]
  # Result: [{c: 3}, {c: 4}] (first x that has an "a" with c=4)

  eval: b?::2
  # Result: 3 (first match, then get index 2)

Variables
~~~~~~~~~~~

If a key or test needs to be a non-string type or contains reserved characters, declare and reference a var instead.

In addition to variables declared in the ``vars`` key and in the `RefContext`, the following variables are always available:

:start: The original "current resource" set by the `RefContext`.
:"true": true,
:"false": false,
:"null": null

Result flattening
~~~~~~~~~~~~~~~~~
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

``x::a::c`` resolves to:
``[1,2,3,4]``
not
``[[1,2], [3,4]])``

(Justification: It is inconvenient and fragile to tie data structures to the particular form of a query.
If you want preserve structure (e.g. to know which values are part
of which parent value or resource) use a less deep path and iterate over results.)
