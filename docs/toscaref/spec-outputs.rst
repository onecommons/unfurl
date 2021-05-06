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

You can view the outputs either by using the CLI.

.. code:: yaml

 cfy deployments outputs DEPLOYMENT_ID

or by making a REST call

.. code:: yaml

 curl -XGET http://MANAGER_IP/deployments/<DEPLOYMENT_ID>/outputs
