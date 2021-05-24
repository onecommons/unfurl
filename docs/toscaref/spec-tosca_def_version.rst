Versioning
==========

``tosca_definitions_version`` is a top level property of the service template which is used to specify the DSL version used. 

Example
+++++++

.. code:: yaml

 tosca_definitions_version: tosca_dsl_1_3

 node_templates:
     ...

The version declaration must be included in the main service template file.


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


DSL Definitions
===============

``dsl_definitions`` section can be used to define arbitrary data structures that can then be reused in different parts of the service templates using YAML anchors and aliases. 

.. seealso:: For more information, refer to :tosca_spec2:`TOSCA DSL Definitions Section <_Toc50125482>`