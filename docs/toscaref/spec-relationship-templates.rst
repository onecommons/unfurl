Relationship Templates
======================

A Relationship Template specifies the relationship between the components defined in the node templates and how the various nodes are connected to each other. Apart from the connections, relationship templates also include information regarding the dependencies and the order of the deployment that should be followed to instantiate a service template.

An important thing to notice here is, in a relationship, it is important for the node requirements of a component to match the capabilities of the node it is being linked to.

Example
++++++++

.. code:: yaml

 relationship_templates:

   storage_attachment:
 
     type: AttachesTo

     properties:

       location: /my_mount_point



.. seealso:: For more information, refer to :tosca_spec2:`TOSCA Relationship Template Section <_Toc50125415>`
