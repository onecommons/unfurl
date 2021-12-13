.. _inputs:

Inputs
======

``inputs`` are parameters injected into the service template upon deployment creation/initiation. These parameters can be referenced by using the :ref:`get_input<get_input>` TOSCA function.

Inputs are useful when there's a need to inject parameters to the service template which were unknown when the service template was created and can be used for distinction between different deployments of the same service template.


Declaration
+++++++++++

.. code:: yaml

 inputs:

   input1:
     ...
   input2:
     ...


Definition
+++++++++++

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
     - An optional description for the input.
   * - type
     - no
     - string
     - Represents the required data type of the input. Not specifying a data type means the type can be anything. Valid types: string, integer, boolean
   * - default
     - no
     - <any>
     - An optional default value for the input.


Example
+++++++

.. code:: yaml

  inputs:

    image_name:
      description: The image name of the server
      type: string
      default: "Ubuntu 12.04"

  node_templates:

    vm:
      type: tosca.openstack.nodes.Server
      properties:
        server:
          image_name: { get_input: image_name }


.. seealso:: :ref:`get_input<get_input>` is a TOSCA function which allows the user to use inputs throughout the service templates. For more information, refer to the :tosca_spec2:`TOSCA Input section <_Toc50125461>`
