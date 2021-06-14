.. _policy:

Policy
========

``policy`` refers to the rule that can be associated with a TOSCA topology or top-level entity definition such as group definition or node template, etc.

Policy Definition
+++++++++++++++++

+------------+----------+--------+-------------------------------------------------+
| Keyname    | Required | Type   | Description                                     |
+============+==========+========+=================================================+
| type       | yes      | string | Policy type.                                    |
+------------+----------+--------+-------------------------------------------------+
| properties | no       | dict   | Optional properties for configuring the policy. |
+------------+----------+--------+-------------------------------------------------+
| targets    | no       | list   | A list of group members. Members are            |
|            |          |        | group or node template names.                   |
+------------+----------+--------+-------------------------------------------------+
| triggers   | no       | dict   | A dict of triggers.                             |
+------------+----------+--------+-------------------------------------------------+

Example
+++++++

.. code:: yaml

 policies:

   - my_compute_placement_policy:

       type: tosca.policies.placement

       description: Apply my placement policy to my application’s servers

       targets: [ my_server_1, my_server_2 ]


.. seealso:: For more information, refer to :tosca_spec2:`TOSCA Policy Section <_Toc50125426>`

Trigger Definition
++++++++++++++++++

+------------+----------+--------+-----------------------------------+
| Keyname    | Required | Type   | Description                       |
+============+==========+========+===================================+
| event       | yes     | string | Event type.                       |
+------------+----------+--------+-----------------------------------+
| action     | no       | list   | The list of sequential activities |
|            |          |        | passed to the trigger.            |
+------------+----------+--------+-----------------------------------+


.. seealso:: For more information, refer to :tosca_spec2:`TOSCA Groups Section <_Toc373867852>`
