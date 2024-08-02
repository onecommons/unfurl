Configuration Files
===================

Unfurl's configuration files are validated according to the JSON schemas described below.
(Note: Required properties are in **bold**.)

Files
-----

.. _ensemble_yaml:

ensemble.yaml
~~~~~~~~~~~~~

Example:

.. include:: examples/ensemble.yaml
   :code: YAML

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

.. code-block:: YAML

    apiVersion: unfurl/v1alpha1
    kind: CloudMap
    repositories:
      unfurl.cloud/onecommons/blueprints/mediawiki:
        git: unfurl.cloud/onecommons/blueprints/mediawiki.git
        path: onecommons/blueprints/mediawiki
        name: MediaWiki
        protocols:
        - https
        - ssh
        internal_id: '35'
        project_url: https://unfurl.cloud/onecommons/blueprints/mediawiki
        metadata:
          description: MediaWiki is free and open-source wiki software used in Wikipedia,
            Wiktionary, and many other online encyclopedias.
          issues_url: https://unfurl.cloud/onecommons/blueprints/mediawiki/-/issues
          homepage_url: https://unfurl.cloud/onecommons/blueprints/mediawiki
        default_branch: main
        branches:
          main: 0fc60cb3afd06ae2c4abe038007e9ff4db398662
        tags:
          v1.0.0: 0fc60cb3afd06ae2c4abe038007e9ff4db398662
          v0.1.0: 225932bc2a45473d682e48f272bc48fcd83909bb
        notable:
          ensemble-template.yaml#spec/service_template:
            artifact_type: artifact.tosca.ServiceTemplate
            name: Mediawiki
            version: 0.1
            description: MediaWiki is free and open-source wiki software used in Wikipedia,
              Wiktionary, and many other online encyclopedias.
            type:
              name: Mediawiki@unfurl.cloud/onecommons/blueprints/mediawiki
              title: Mediawiki
              extends:
              - Mediawiki@unfurl.cloud/onecommons/blueprints/mediawiki
              - SQLWebApp@unfurl.cloud/onecommons/std:generic_types
              - WebApp@unfurl.cloud/onecommons/std:generic_types
              - App@unfurl.cloud/onecommons/std:generic_types
            dependencies:
            - MySQLDB@unfurl.cloud/onecommons/std:generic_types
            artifacts:
            - docker.io/bitnami/mediawiki
    ...
    artifacts:
      docker.io/bitnami/mediawiki:
        type: artifacts.OCIImage


JSON Schema:

.. jsonschema:: cloudmap-schema.json


Sections
---------

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
