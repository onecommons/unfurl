Configuration Files
===================

Unfurl's configuration files are validated according to the JSON schemas described below.
(Note: Required properties are in **bold**.)

Files
-----

.. jsonschema:: manifest-schema.json


.. jsonschema:: unfurl-schema.json


.. jsonschema:: changelog-schema.json


Sections
---------

.. jsonschema:: manifest-schema.json#/definitions/instance

.. jsonschema:: manifest-schema.json#/definitions/import

.. jsonschema:: manifest-schema.json#/definitions/status

.. jsonschema:: manifest-schema.json#/definitions/job

.. jsonschema:: manifest-schema.json#/definitions/task

.. jsonschema:: manifest-schema.json#/definitions/configurationSpec

.. jsonschema:: manifest-schema.json#/definitions/changes


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
