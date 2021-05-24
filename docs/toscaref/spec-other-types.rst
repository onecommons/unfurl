Other Types
===========

This section includes the brief description for the following:

* :ref:`Artifact Types<artifact_types>`
* :ref:`Capability Types<capability_types>`
* :ref:`Relationship Types<relationship_types>`
* :ref:`Interface Types<interface_types>`
* :ref:`Group Types<group_types>`
* :ref:`Policy Types<policy_types>`

.. _artifact_types:

Artifact Types
+++++++++++++++

``artifact_types`` is a reusable entity in a servcie template that defines the type of one or more files that are used to define implementation or deployment artifacts. These are referenced by nodes or relationships on their operation. 

Example
--------

.. code:: yaml

 artifact_types:
   QCOW:
     derived_from: tosca:Deployment.Image.VM
     properties:
       os:
         type: string
       version:
         type: version
     mime_type: application/x-qcow
     file_ext: [ qcow, qcow2 ]


.. seealso:: For more information, refer to the :tosca_spec2:`TOSCA Artifact Type section <_Toc50125358>`


.. _capability_types:

Capability Types
+++++++++++++++++

``capability_types`` is a reusable entity that describes a kind of capability that a node type can declare to expose.  Implicit or explicit requirements that are declared as part of one node can be matched or fulfilled by  the capabilities declared by another node.

Example
--------

.. code:: yaml

 capability_types:
   Socket:
     properties:
       standard:
         type: string

.. seealso:: For more information, refer to the :tosca_spec2:`TOSCA Capability Type section <_Toc50125375>`

.. _relationship_types:

Relationship Types
+++++++++++++++++++

``relationship_types`` is a reusable entity that defines the type of one or more relationships between Node Types or Node Templates.

Example
--------

.. code:: yaml

 relationship_types:
   Plug:
     properties:
       vendor:
         type: string
         required: false
     attributes:
       ip_address:
         type: string

.. seealso:: For more information, refer to the :tosca_spec2:`TOSCA Relationship Types section <_Toc50125386>`

.. _interface_types:

Interface Types
++++++++++++++++

``interface_types`` is a reusable entity that describes a set of operations that can be used to interact with or manage a node or relationship in a TOSCA topology.

Example
--------

.. code:: yaml

 interface_types:
     Backup:
       operations:
         start_backup: {}

.. seealso:: For more information, refer to the :tosca_spec2:`TOSCA Interface Types section <_Toc50125364>`

.. _group_types:

Group Types
+++++++++++

``group_types`` defines logical grouping types for nodes.

Example
--------

.. code:: yaml

 group_types:
  ResourceGroup:
     # Groups can have properties
     properties:
       priority:
         type: float
     members: # node or group types
     - tosca:Compute
     - tosca:Abstract.Storage


.. seealso:: For more information, refer to the :tosca_spec2:`TOSCA Group Types section <_Toc50125391>`

.. _policy_types:

Policy Types
+++++++++++++

``policy_types`` defines a type of requirement that affects or governs an application or service’s topology at some stage of its lifecycle, but is not explicitly part of the topology itself.

Example
--------

.. code:: yaml

 policy_types:
   Backup:
     targets:
     # Can include both node types and group types
     - tosca:Compute
     - RedundantResources # This group type is declared below
     # If “targets” is not specified then any node template or group can be a target


.. seealso:: For more information, refer to the :tosca_spec2:`TOSCA Policy Types section <_Toc50125397>`

