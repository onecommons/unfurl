Policy
========

``policy`` refers to the rule that can be associated with a TOSCA topology or top-level entity definition such as group definition or node template, etc.

Example
+++++++

.. code:: yaml

 policies:

   - my_compute_placement_policy:

       type: tosca.policies.placement

       description: Apply my placement policy to my application’s servers

       targets: [ my_server_1, my_server_2 ]



.. seealso:: For more information, refer to :tosca_spec2:`TOSCA Policy Section <_Toc50125426>`