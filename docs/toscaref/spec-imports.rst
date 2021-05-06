Imports
=======

``imports`` allow the author of a service template to reuse service template files or
parts of them and use predefined types (e.g. from the types.yaml file).

Declaration
+++++++++++

.. code:: yaml

 imports:
   - ...
   - ...

Example
++++++++

.. code:: yaml

 imports:
   - {{< field "types_yaml_link" >}}
   - my_yaml_files/openstack_types.yaml

 node_templates:
   vm:
     type: tosca.openstack.nodes.Server
   webserver:
     type: tosca.nodes.WebServer

In the above example, we import the default types.yaml file provided by
TOSCA which contains the ``tosca.nodes.WebServer`` :ref:`Node Types <node_types>` and a custom YAML created
for custom OpenStack plugin containing the
``tosca.openstack.nodes.Server`` node type.

A few important things to know about importing YAML files:

* Imported files can be either relative to the service templates’s root directory or be a URL (as seen above).
* You can use imports within imported files and nest as many imports as you like.
* An error will be raised if there are cyclic imports (i.e. a file is importing itself or importing a file which is importing the file that imported it, etc..)
* The following parts of the DSL cannot be imported and can only be defined in the main service template file:

   * :ref:`Groups<groups>`
   * :ref:`Inputs<inputs>`
   * :ref:`Node Templates<node_templates>`
   * :ref:`Outputs<outputs>`

* The ``tosca_definitions_version`` must match between imported files.
