.. _substitution_mapping:

Substitution Mapping
====================

A substitution mapping allows a given topology template to be used as an implementation of abstract node templates of a specific node type. This allows the consumption of complex systems using a simplified vision.

Declaration
++++++++++++

.. code:: yaml

 node_type: <node_type_name>

 substitution_filter : <node_filter>

 properties:

   <property_mappings>
 
 capabilities:

   <capability_mappings>

 requirements:

   <requirement_mappings>

 attributes:

   <attribute_mappings>

 interfaces:

   <interface_mappings>

.. seealso:: For more information, refer to :tosca_spec2:`TOSCA Substitution Mapping Section <_Toc50125452>`