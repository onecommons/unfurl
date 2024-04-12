.. _substitution_mappings:

Substitution Mappings
=====================

TOSCA lets you replace a node template in one topology with another topology in its entirety.
To make a topology available for substitution, add a ``substitution_mappings`` section to the topology template.
To use this topology in another topology, declare a node template with a ``substitute`` directive and Unfurl will find a matching topology to replace the node template.

The ``substitution_mappings`` section describes how to map the topology to the substituted node template's type, properties, attributes, and requirements.
As a TOSCA extension, Unfurl adds a ``node`` key to point to a node template in that topology to be used to replace the substituted node template in the other topology.
This way, you don't need to provide a full mapping.

In addition, ``root`` can be used as an alias for the ``substitution_mappings`` keyword.
This is also used by Unfurl to figure out which node templates to include in the plan for a deployment job.
If it is present, only node templates reachable from that root template will be included in the plan.
If it is not declared, all node templates in the topology will be included.


Declaration
++++++++++++

.. code:: yaml

 substitution_mappings:  # or root:

    node: <node_template_name>  # unfurl extension

    node_type: <node_type_name>

    substitution_filter: <node_filter>

    properties:  # mapping to map topology inputs to node template properties

      <property_mappings>

    attributes: # mappings to map topology outputs to node template attributes

      <attribute_mappings>

    requirements:

      <requirement_mappings>

    capabilities:   # currently unsupported

      <capability_mappings>

    interfaces:    # currently unsupported

      <interface_mappings>

.. seealso:: For more information, refer to :tosca_spec2:`TOSCA Substitution Mapping Section <_Toc50125452>`