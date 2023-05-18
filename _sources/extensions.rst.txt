Unfurl Specific
~~~~~~~~~~~~~~~

See also the built-in `Unfurl types`.

Extensions
^^^^^^^^^^^

* add 'any' schema type for properties and attributes definitions
* 'additionalProperties' field in type and template metadata
* allow metadata field on inputs, outputs, artifacts, and repositories
* add "sensitive" property and datatype metadata field
* add "immutable" property metadata field
* add "environment" keyword to implementation definition
* add "eval" function
* add "type" in capability assignment
* allow workflows to be imported
* workflow "target" keyword also accepts type names
* groups can have other groups as members
* an operation's ``operation_host`` field can also be set to a node template's name.
* added ``OPERATION_HOST`` as a reserved function keyword.
* add "discover", "default", "dependent", "virtual", and "protected" directives
* add "default_for" keyword to relationship templates
* add "defaults" section to interface definitions
* add "requirements" keyword to interface definitions
* add "types" section to the service template can contain any entity type definition.
* add "when" keyword to "imports" to allow conditional imports
* add "decorators" section for rule-based enhancements of node templates
* add "requirements" section to node_filters
* allow "root" as alias for ``substitution_mappings``
* add "node" keyword to ``substitution_mappings`` section
* add ``not_implemented``, ``invoke``, ``entry_state`` keyword for operations (so an operation aren't inherited)
* allow ``TOSCAVersion`` to accept Semantic Versioning 2.0 syntax
* allow "required" key in artifact definitions on node types
* allow "revision" keyword in repository definitions
* allow "imported" key on template definitions
* ``derived_from`` can accept a list types, enabling multiple inheritance.

Not yet implemented
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The following feature are either not yet implemented or don't currently
conform with the TOSCA 1.3 specification:

* The ``get_operation_output`` function (use :ref:`resultTemplate<resulttemplate>` instead)
* "copy" keyword (use the ``dsl`` section or :ref:`merge directives<yaml_merge_directives>` instead)
* `get_artifact` function (only implemented for artifacts that are container images)
* CSAR manifests and archives (implemented but untested)
* substitution mappings: only input mappings are supported (set ``node`` to a node template instead)
* triggers
* notifications
* xml schema constraints
