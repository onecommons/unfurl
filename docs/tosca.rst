TOSCA support
=============

The service templates are defined using OASIS's `TOSCA <https://www.oasis-open.org/committees/tc_home.php?wg_abbrev=tosca>`_
("Topology and Orchestration Specification for Cloud Applications") Simple Profile
in YAML Version 1.3 standard (`html <https://docs.oasis-open.org/tosca/TOSCA-Simple-Profile-YAML/v1.3/TOSCA-Simple-Profile-YAML-v1.3.html>`_)
(`pdf <https://docs.oasis-open.org/tosca/TOSCA-Simple-Profile-YAML/v1.3/TOSCA-Simple-Profile-YAML-v1.3.pdf>`_)
with the follow differences:

Extensions
~~~~~~~~~~

* add 'any' schema type for properties and attributes definitions
* 'additionalProperties' field in type metadata
* allow metadata field on parameters (i.e. inputs and outputs) and on artifacts
* "sensitive" property and datatype metadata field
* "immutable" property metadata field
* add "environment" keyword to implementation definition
* add "eval" function
* add "type" in capability assignment
* allow workflow to be imported
* workflow "target" accepts type names
* groups can have other groups as members
* operation_host can also refer to a node template name
* add OPERATION_HOST reserved function keywords
* add "discover" and "default" directives

Not yet implemented and non-conformance with the TOSCA 1.3 specification
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

* Operation Outputs (use `resultTemplate` instead)
* "copy" keyword (use dsl or merge directives instead)
* `get_attribute` function (use `eval` instead)
* `get_nodes_of_type` (use `eval` instead)
* `get_artifact` function
* CSAR manifests and archives (implemented but untested)
* substitution mapping (mostly implemented but untested)
* triggers
* notifications
* node_filters
* xml schema constraints

Extensions to built-in definitions
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. include:: tosca-ext.yaml
   :code: YAML
