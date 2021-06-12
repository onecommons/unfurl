Unfurl Specific
~~~~~~~~~~~~~~~

See also the built-in `Unfurl types`.

Extensions
^^^^^^^^^^^

* add 'any' schema type for properties and attributes definitions
* 'additionalProperties' field in type metadata
* allow metadata field on inputs, outputs, artifacts, and repositories
* add "sensitive" property and datatype metadata field
* add "immutable" property metadata field
* add "environment" keyword to implementation definition
* add "eval" function
* add "type" in capability assignment
* allow workflows to be imported
* workflow "target" keyword also accepts type names
* groups can have other groups as members
* An operation's ``operation_host`` field can also be set to a node template's name.
* added ``OPERATION_HOST`` as a reserved function keyword.
* add "discover" and "default" directives
* add "default_for" keyword to relationship templates
* add "defaults" section to interface definitions
* add "types" section to the service template can contain any entity type definition.

Not yet implemented 
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The following feature are either not yet implemented or don't currently 
conform with the TOSCA 1.3 specification:

* The ``get_operation_output`` function (use :ref:`resultTemplate<resulttemplate>` instead)
* "copy" keyword (use the ``dsl`` section or :ref:`merge directives<yaml_merge_directives>` instead)
* `get_artifact` function (only implemented for artifacts that are container images)
* CSAR manifests and archives (implemented but untested)
* substitution mapping
* triggers
* notifications
* node_filters
* xml schema constraints
