.. _outputs:

Outputs
=======

``outputs`` provide a way of exposing global aspects of a deployment.
When deployed, a service template can expose specific outputs of that
deployment - for instance, an endpoint of a server or any other runtime
or static information of a specific resource.

Declaration
++++++++++++

.. code:: yaml

 outputs:
   output1:
     ...
   output2:
     ...

Definition
++++++++++++

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
     - An optional description for the output.
   * - values
     - yes
     - <any>
     - The output value. May be anything from a simple value (e.g. port) to a complex value (e.g. hash with values). Output values can contain hard-coded values, inputs, properties and attributes.


Example
++++++++

.. code:: yaml

 tosca_definitions_version: tosca_dsl_1_3

 imports:
   - http://<example URL>/types.yaml

 node_templates:
   webserver_vm:
     type: tosca.nodes.Compute
   webserver:
     type: tosca.nodes.WebServer
     properties:
         port: 8080

 outputs:
     webapp_endpoint:
         description: ip and port of the web application
         value:
             ip: { get_attribute: [webserver_vm, ip] }
             port: { get_property: [webserver, port] }

Outputs
+++++++

Outputs are recorded in the :ref:`ensemble.yaml<ensemble_yaml>` and is printed in the console after a job has completed as part of the job summary.

.. code:: yaml

 status:
   outputs:
     url: https://demo.example.com
   webapp_endpoint:
        description: ip and port of the web application
        value:
            ip: { get_attribute: [webserver_vm, ip] }
            port
 
Example
-------

.. code:: yaml

 status:
   outputs:
     webapp_endpoint:
       ip: 127.0.0.1
       port: 8080


.. seealso:: For more information, refer to :tosca_spec2:`TOSCA Output Section <_Toc50125464>`

