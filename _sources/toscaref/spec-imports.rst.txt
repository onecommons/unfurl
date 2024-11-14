.. _tosca_imports:

Imports
=======

``imports`` allow the author of a service template to reuse service template files or
parts of them and use predefined types (e.g. from the types.yaml file).

Declaration
+++++++++++



.. code:: yaml

 imports:
   - file: <file_path>
     repository: <repository_name>
     namespace_prefix: <definition_namespace_prefix>
   - <url>  # short form of import
  
Example
++++++++

.. code:: yaml

 imports:
   - my_yaml_files/example_types.yaml

 node_templates:
   vm:
     type: example.nodes.Server
   webserver:
     type: tosca.nodes.WebServer

In the above example, we import the default types.yaml file provided by
TOSCA which contains the ``tosca.nodes.WebServer`` :ref:`Node Types <node_types>` and a custom YAML created
for custom OpenStack plugin containing the
``example.nodes.Server`` node type.

A few important things to know about importing YAML files:

* Imported files can be either relative to the service templates’s root directory, to its declared repository, or be a URL (as seen above).
* You can use imports within imported files and nest as many imports as you like.
* Import only makes available the imported type definitions, not the topology template. To access an imported topology use `Substitution Mappings`. 

.. seealso:: For more information, refer to :tosca_spec2:`TOSCA Import Section <_Toc50125256>`

