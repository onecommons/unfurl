.. _groups:

Groups
======

``groups`` provide a way of configuring shared behavior for different
sets of ``node_templates``.

Declaration
+++++++++++

Within each group, the ``policies`` section is a dictionary where each
item in the dictionary represents a policy.

Within each policy, the ``triggers`` section is a dictionary where each
item in the dictionary represents a trigger.

.. code:: yaml

 groups:
   group1:
     members: ...
     policies:
       policy1:
         type: ...
         properties:
           ...
         triggers:
           trigger1:
             type: ...
             parameters: ...
           trigger2:
             ...
       policy2:
         ...
   group2:
     ...



Definition
+++++++++++

+----------+----------+------+--------------------------------------+
| Keyname  | Required | Type | Description                          |
+==========+==========+======+======================================+
| members  | yes      | list | A list of group members. Members are |
|          |          |      | node template names.                 |
+----------+----------+------+--------------------------------------+
| policies | yes      | dict | A dict of policies.                  |
+----------+----------+------+--------------------------------------+

Policy Definition
+++++++++++++++++

+------------+----------+--------+-------------------------------------------------+
| Keyname    | Required | Type   | Description                                     |
+============+==========+========+=================================================+
| type       | yes      | string | Policy type.                                    |
+------------+----------+--------+-------------------------------------------------+
| properties | no       | dict   | Optional properties for configuring the policy. |
+------------+----------+--------+-------------------------------------------------+
| triggers   | yes      | dict   | A dict of triggers.                             |
+------------+----------+--------+-------------------------------------------------+

Trigger Definition
++++++++++++++++++

+------------+----------+--------+----------------------------------+
| Keyname    | Required | Type   | Description                      |
+============+==========+========+==================================+
| type       | yes      | string | Trigger type.                    |
+------------+----------+--------+----------------------------------+
| parameters | no       | dict   | Optional parameters that will be |
|            |          |        | passed to the trigger.           |
+------------+----------+--------+----------------------------------+

|

Inside the trigger’s ``parameters`` section,
``{ get_property: [SELF, property_name] }`` can be used to access
properties of the event that caused the trigger to be processed. For
example, a policy may add contextual data to an event, such as a node
instance id or the CPU average in the last five minutes, before
processing its triggers. An ``execute_workflow`` trigger, for example,
may pass these properties to the workflow it executes.


