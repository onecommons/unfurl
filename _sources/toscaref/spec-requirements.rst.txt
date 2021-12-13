.. _requirements:


Requirements
============

Requirements let you define how nodes relate to one another. For example, a ``web_server`` node can be ``contained_in`` a VM node or an application node can be ``connected_to`` a database node.


Declaration
+++++++++++

.. code:: yaml

 <requirement_definition_name>:

   capability: <capability_type_name>

   node: <node_type_name>

   relationship: <relationship_type_name>

   occurrences: [ <min_occurrences>, <max_occurrences> ]

.. seealso:: For more information, refer to :tosca_spec2:`TOSCA Requirement Section <DEFN_ELEMENT_REQUIREMENT_DEF>`

