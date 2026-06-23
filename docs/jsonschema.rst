Configuration Files
===================

Unfurl's configuration files are validated according to the JSON schemas described below.
(Note: Required properties are in **bold**.)

.. contents::
   :local:
   :depth: 2

Files
-----


ensemble.yaml
~~~~~~~~~~~~~

Example:

.. include:: examples/ensemble.yaml
   :code: YAML

.. _ensemble_yaml:

JSON Schema:

.. jsonschema:: manifest-schema.json


job.yaml
~~~~~~~~

Example:

.. include:: examples/job.yaml
   :code: YAML

JSON Schema:

.. jsonschema:: changelog-schema.json


unfurl.yaml
~~~~~~~~~~~

Example:

.. include:: examples/unfurl.yaml
   :code: YAML

JSON Schema:

.. jsonschema:: unfurl-schema.json


cloudmap.yaml
~~~~~~~~~~~~~

Example (from https://github.com/onecommons/cloudmap/blob/main/cloudmap.yaml):

.. literalinclude:: examples/cloudmap.yaml
   :language: yaml


JSON Schema:

.. jsonschema:: cloudmap-schema.json


Sections
---------

.. _environment_schema:

.. jsonschema:: manifest-schema.json#/definitions/environment

.. jsonschema:: manifest-schema.json#/definitions/instance

.. jsonschema:: manifest-schema.json#/definitions/external

.. jsonschema:: manifest-schema.json#/definitions/repositories

.. jsonschema:: manifest-schema.json#/definitions/status

.. jsonschema:: manifest-schema.json#/definitions/job

.. jsonschema:: manifest-schema.json#/definitions/task

.. jsonschema:: manifest-schema.json#/definitions/configurationSpec

.. jsonschema:: manifest-schema.json#/definitions/changes

.. jsonschema:: manifest-schema.json#/definitions/lock


Definitions
-----------

Enums and Simple Types
~~~~~~~~~~~~~~~~~~~~~~

.. jsonschema:: manifest-schema.json#/definitions/readyState

.. jsonschema:: manifest-schema.json#/definitions/state

.. jsonschema:: manifest-schema.json#/definitions/changeId

.. jsonschema:: manifest-schema.json#/definitions/timestamp

.. jsonschema:: manifest-schema.json#/definitions/version

Reusable helper definitions
~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. jsonschema:: manifest-schema.json#/definitions/instances

.. jsonschema:: manifest-schema.json#/definitions/attributes

.. jsonschema:: manifest-schema.json#/definitions/atomic

.. jsonschema:: manifest-schema.json#/definitions/namedObjects

.. jsonschema:: manifest-schema.json#/definitions/schema


Cloudmap definitions
--------------------

.. jsonschema:: cloudmap-schema.json#/definitions/typeRef

.. jsonschema:: cloudmap-schema.json#/definitions/typedURLs

.. jsonschema:: cloudmap-schema.json#/definitions/metadata

.. jsonschema:: cloudmap-schema.json#/definitions/discovery

.. jsonschema:: cloudmap-schema.json#/definitions/relationships

.. jsonschema:: cloudmap-schema.json#/definitions/repository

.. jsonschema:: cloudmap-schema.json#/definitions/service

.. jsonschema:: cloudmap-schema.json#/definitions/artifact

.. jsonschema:: cloudmap-schema.json#/definitions/component

.. jsonschema:: cloudmap-schema.json#/definitions/lifecycle_status

.. jsonschema:: cloudmap-schema.json#/definitions/release_schedule

.. jsonschema:: cloudmap-schema.json#/definitions/instantiation

.. jsonschema:: cloudmap-schema.json#/definitions/type

.. jsonschema:: cloudmap-schema.json#/definitions/inlineArtifact
