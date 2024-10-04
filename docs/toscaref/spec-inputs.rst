Inputs
======

`inputs<Topology Inputs>` are parameters passed to a service template when it is instantiated. These parameters can be referenced by using the :ref:`get_input<get_input>` TOSCA function.

Inputs are useful when there's a need to inject parameters to the service template which were unknown when the service template was created and can vary across different deployments of the same service template.

See `Topology Inputs` on how to pass inputs to a service template.

Declaration
+++++++++++

.. code:: yaml

  topology_template:
    inputs:

      input1:
        type:
        ...
      input2:
        type:
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
     - Represents the required data type of the input. Not specifying a data type means the type can be anything.
   * - default
     - no
     - <any>
     - An optional default value for the input.

.. seealso:: For the full specification, see the :tosca_spec2:`TOSCA Input section <_Toc50125461>`.

Example
+++++++

.. tab-set-code::

  .. literalinclude:: ../examples/inputs.yaml
    :language: yaml

  .. literalinclude:: ../examples/inputs.py
    :language: python


