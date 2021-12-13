.. _groups:

Groups
======

``groups`` provide a way of configuring shared behavior for different
sets of ``node_templates``.

Example
+++++++

.. code:: yaml

 groups:
   group1:
     members:
       - node_template_1
       - another_group


Definition
+++++++++++

+------------+----------+------+--------------------------------------+
| Keyname    | Required | Type | Description                          |
+============+==========+======+======================================+
| members    | yes      | list | A list of group members. Members are |
|            |          |      | group or node template names.        |
+------------+----------+------+--------------------------------------+
| properties | no       | dict | A dict of properties.                |
+------------+----------+------+--------------------------------------+

