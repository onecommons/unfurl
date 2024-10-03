.. _groups:

Groups
======

``groups`` provide a way of configuring shared behavior for different
sets of ``node_templates``.  Groups can optionally have a :ref:`type <group_types>` and properties.

As an Unfurl extension, groups can have other groups as members.

Example
+++++++

.. code:: yaml

 groups:
   group1:
     type: my_group_type
     members:
       - node_template_1
       - another_group
   another_group:
     members:
       - node_template_2

Definition
+++++++++++

+------------+----------+------+--------------------------------------+
| Keyname    | Required | Type | Description                          |
+============+==========+======+======================================+
+------------+----------+------+--------------------------------------+
| type       | no       |string| The group's :ref:`type <group_types>`|
+------------+----------+------+--------------------------------------+
| members    | yes      | list | A list of group members. Members are |
|            |          |      | group or node template names.        |
+------------+----------+------+--------------------------------------+
| properties | no       | dict | A dict of properties.                |
+------------+----------+------+--------------------------------------+
