=========================
Metadata Elements
=========================

Version
=========================

``tosca_definitions_version`` is a top level element of the service template which is used to specify the version of TOSCA to be used. 

Use ``tosca_simple_unfurl_1_0_0`` to enable Unfurl extensions or the standard ``tosca_simple_yaml_1_3``.

Example
+++++++

.. code:: yaml

 tosca_definitions_version: tosca_simple_unfurl_1_0_0

 node_templates:
     ...

The version declaration must be included in the main service template file.

Description
===========

This optional element provides a means to include single or multiline description of the template.

Example
+++++++

.. code:: yaml

    description: >

    This is an example of a multi-line description using YAML. It permits line       

      breaks for easier readability...


Metadata
========

``metadata`` allows a declaration of a map of keynames with string values. It is used to associate domain-specific metadata with the Service Template. 


Example
++++++++

.. code:: yaml

 metadata:

   creation_date: 2015-04-14
   date_updated: 2015-05-01
   status: developmental 

While this is just an example, it is recommended that you set the following ``metadata``:

.. code:: yaml

 metadata:

  template_name: example
  template_author: <author>
  template_version: 1.0.0


DSL Definitions
===============

``dsl_definitions`` section can be used to define arbitrary data structures that can then be reused in different parts of the service templates using YAML anchors and aliases. 

.. seealso:: For more information, refer to :tosca_spec2:`TOSCA DSL Definitions Section <_Toc50125482>`