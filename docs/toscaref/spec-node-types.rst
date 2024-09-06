.. _node_types:

Node Types
===========

``node_types`` are used for defining common properties and behaviors for :ref:`Node Templates <node_templates>`.
``node-templates`` can then be created based on these types, inheriting their definitions.

A partial definition (type only) is treated a requirement that the node template declares an artifact of that name and type.

Declaration
++++++++++++

.. code:: yaml

 node_types:

  type1:
    derived_from: tosca.nodes.Root
    properties:
      ...
    interfaces:
      ...

  type2:
    ...
  ...


Definition
++++++++++++

============ ======== ==============   =====================================
Keyname      Required Type             Description
============ ======== ==============   =====================================
derived_from yes      string or list   Parent type(s).
metadata     no       dictionary       A dictionary of node metadata.
properties   no       dictionary       A dictionary of node properties.
attributes   no       dictionary       A dictionary of node attributes.
capabilities no       dictionary       A dictionary of node capabilities.
requirements no       dictionary       A dictionary of node requirements.
artifacts    no       dictionary       A dictionary of artifact definitions.
interfaces   no       dictionary       A dictionary of node interfaces.
============ ======== ==============   =====================================

derived_from
------------

The ``derived_from`` property may be used to build over and extend an
existing type. This is useful for further extracting common properties
and behaviors, this time in between *types*.

Using this mechanism, one can build various type hierarchies which can
be reused over different application service templates.

As a TOSCA extension, Unfurl support multiple inheritance if a ``derived_from`` if a list of type names.

When a type derives from another type, its ``interfaces`` and
``properties`` keys get merged with the parent type’s ``interfaces`` and
``properties`` keys. The merge is on the property/operation level: A
property defined on the parent type will be overridden by a property
with the same name defined on the deriving type. The same is true for an
interface operation mapping - however, it is important to note that it’s
possible to add in the deriving type additional operation mappings to an
interface defined in the parent type. See the `examples
section <#examples>`__ for more on this.


properties
------------

The ``properties`` property may be used to define a common properties
schema for node templates.

``properties`` is a dictionary from a property name to a dictionary
describing the property. The nested dictionary includes the following
keys:

.. list-table::
   :widths: 10 10 10 50
   :header-rows: 1

   * - Keyname
     - Required
     - Type
     - Description
   * - description
     - no
     - string
     - Description for the property.
   * - type
     - no
     - string
     - Property type. Not specifying a data type means the type can be anything (including types not listed in the valid types). Valid types: string, integer, float, boolean or a :ref:` Custom Data Types <data_types>`.
   * - default
     - no
     - <any>
     - An optional default value for the property.
   * - required
     - no
     - boolean
     - Specifies whether the property is required.

artifacts
---------

As a TOSCA extension, Unfurl let's you define `artifacts` on node types in addition to node templates.
If the artifact is partially defined (``type`` only), then node templates will be required to declare an artifact of that name and type.
If the artifact has a complete definition it will be treated as if it was declared on node templates that match this node type.

interfaces
------------

The ``interfaces`` property may be used to define common behaviors for
node templates.

.. seealso::

 For more information, please refer to the :ref:`Interfaces<interfaces_spec>` section.


Examples
*********

.. tab-set-code::

  .. code:: yaml

    node_types:
      nodecellar.nodes.MongoDatabase:
        derived_from: tosca.nodes.DBMS
        properties:
          port:
            description: MongoDB port
            type: integer
        interfaces:
          Standard:
            create: scripts/mongo/install-mongo.sh
            start: scripts/mongo/start-mongo.sh
            stop: scripts/mongo/stop-mongo.sh

  .. literalinclude:: ./../examples/node-types-1.py
    :language: python

An example of how to use this type follows:

.. code:: yaml

 node_templates:
   MongoDB1:
     type: nodecellar.nodes.MongoDatabase
   MongoDB2:
     type: nodecellar.nodes.MongoDatabase


Each of these two nodes will now have both the ``port`` property and the three operations defined for the ``nodecellar.nodes.MongoDatabase`` type.

Finally, an example on how to extend an existing type by deriving from it:

.. tab-set-code::

  .. code:: yaml

    node_types:
      nodecellar.nodes.MongoDatabaseExtended:
        derived_from: nodecellar.nodes.MongoDatabase
        properties:
          enable_replication:
            description: MongoDB replication enabling flag
            type: boolean
            default: false
        interfaces:
          Standard:
            create: scripts/mongo/install-mongo-extended.sh
            configure: scripts/mongo/configure-mongo-extended.sh

  .. literalinclude:: ./../examples/node-types-2.py
    :language: python


The ``nodecellar.nodes.MongoDatabaseExtended`` type derives from the ``nodecellar.nodes.MongoDatabase`` type which was defined in the previous example; As such, it derives its properties and interfaces definitions, which get either merged or overridden by the ones it defines itself.

A node template whose type is ``nodecellar.nodes.MongoDatabaseExtended`` will therefore have both the ``port`` and ``enable_replication`` properties, as well as the following interfaces mapping:

.. code:: yaml

     interfaces:
       Standard:
         create: scripts/mongo/install-mongo-extended.sh
         configure: scripts/mongo/configure-mongo-extended.sh
         start: scripts/mongo/start-mongo.sh
         stop: scripts/mongo/stop-mongo.sh


As it is evident, the ``configure`` operation, which is mapped only in the extending type, got merged with the ``start`` and ``stop`` operations which are only mapped in the parent type, while the ``create`` operation, which is defined on both types, will be mapped to the value set in the extending type.

.. seealso:: For more information, refer to :tosca_spec2:`TOSCA Node Types Section <_Toc50125490>`
