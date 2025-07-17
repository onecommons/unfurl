.. _topology_outputs:

Outputs
=======

When deployed, a service template can expose specific ``outputs`` of that
deployment - for instance, an ip address of a server or any other runtime
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
   * - value
     - yes
     - <any>
     - The output value. May be anything from a simple value (e.g. port) to a complex value (e.g. hash with values). Output values can contain hard-coded values, inputs, properties and attributes.


Example
++++++++

.. code:: yaml

 topology_template:
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

Job Status
++++++++++

Outputs are printed in the console after a job has completed as part of the job summary and recorded in the :ref:`ensemble.yaml<ensemble_yaml>`'s status section:

.. code:: yaml

 status:
   outputs:
     webapp_endpoint:
       ip: 127.0.0.1
       port: 8080


.. seealso:: For more information, refer to :tosca_spec2:`TOSCA Output Section <_Toc50125464>`

