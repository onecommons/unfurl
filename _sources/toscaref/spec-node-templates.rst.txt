.. _node_templates:

Node Templates
==============

``node_templates`` represent the actual instances of :ref:`Node Types <node_types>` which would eventually
represent a running application/service as described in the service template.

Declaration
-----------

The ``node_templates`` section in the DSL is a dictionary where each key
is a node template.

.. code:: yaml

   node_templates:

     node_template_1:
       type: ...
       properties:
         ...
       interfaces:
         ...
       requirements:
         ...

     node_template_2:
       ...

Definition
----------

.. list-table:: 
   :widths: 10 10 10 50
   :header-rows: 1

   * - Keyname
     - Required
     - Type
     - Description
   * - type
     - yes
     - string
     - The :ref:`node_type <node_types>` of this node template.
   * - properties
     - no
     - dict
     - The properties of the node template matching its node type properties schema.
   * - interfaces
     - no
     - interfaces
     - Used for mapping plugins to :ref:`interfaces<interfaces>` operation or for specifying inputs for already mapped node type operations.
   * - requirements
     - no
     - requirements
     - Used for specifying the :ref:`requirements<requirements>` this node template has.


Example
-------

.. code:: yaml

   node_types:
     # The following node type is used in the node templates section
     nodes.Nginx:
       derived_from: tosca.nodes.WebServer
       properties:
         port:
           description: The default listening port for the Nginx server.
           type: integer
       interfaces:
         Standard:
           create:
             implementation: scripts/install-nginx.sh
             inputs:
               process:
                 default:
                   env:
                     port: 80
           start: scripts/start-nginx.sh

   node_templates:
     vm:
       type: tosca.nodes.Compute
       properties:
         ip: 192.168.0.11

     nginx:
       # We specify that this node template is of the node type we defined in the node types section
       type: nodes.Nginx
       # properties should match nodes.Nginx type properties schema
       properties:
         port: 80
       interfaces:
         Standard:
           create:
             # inputs should match the inputs schema defined in nodes.Nginx for the create operation
             inputs:
               process:
                 env:
                   port: { get_property: [SELF, port] }
       requirements:
         - type: tosca.requirements.contained_in
           target: vm


.. seealso:: For more information, refer to :tosca_spec2:`TOSCA Node Templates Section <_Toc50125410>`
