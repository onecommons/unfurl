TOSCA
=====

The service templates are defined using OASIS's `TOSCA <https://www.oasis-open.org/committees/tc_home.php?wg_abbrev=tosca>`_
("Topology and Orchestration Specification for Cloud Applications") Simple Profile
in YAML Version 1.3 standard (`html <https://docs.oasis-open.org/tosca/TOSCA-Simple-Profile-YAML/v1.3/TOSCA-Simple-Profile-YAML-v1.3.html>`_)
(`pdf <https://docs.oasis-open.org/tosca/TOSCA-Simple-Profile-YAML/v1.3/TOSCA-Simple-Profile-YAML-v1.3.pdf>`_)
with the follow differences:

Extensions
~~~~~~~~~~

* add 'any' schema type for properties and attributes definitions
* 'additionalProperties' field in type metadata
* allow metadata field on inputs, outputs, artifacts, and repositories
* add "sensitive" property and datatype metadata field
* add "immutable" property metadata field
* add "environment" keyword to implementation definition
* add "eval" function
* add "type" in capability assignment
* allow workflow to be imported
* workflow "target" accepts type names
* groups can have other groups as members
* An operation's ``operation_host`` field can also be set to a node template's name.
* added ``OPERATION_HOST`` as a reserved function keyword.
* add "discover" and "default" directives
* add "default_for" keyword to relationship templates
* add "defaults" section to interface definitions

Not yet implemented and non-conformance with the TOSCA 1.3 specification
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

* The ``get_operation_output`` function (use `resultTemplate` instead)
* "copy" keyword (use the ``dsl`` section or `merge directives` instead)
* `get_artifact` function (only implemented for artifacts that are container images)
* CSAR manifests and archives (implemented but untested)
* substitution mapping
* triggers
* notifications
* node_filters
* xml schema constraints

Extensions to built-in definitions
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. include:: tosca-ext.yaml
   :code: YAML
