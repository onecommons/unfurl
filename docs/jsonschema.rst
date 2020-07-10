Configuration Files
===================

Unfurl's configuration files are validated according to the JSON schemas described below.
(Note: Required properties are in **bold**.)

Files
-----

manifest.yaml
~~~~~~~~~~~~~

.. code-block:: YAML

  apiVersion: VERSION
  kind: Manifest
  # the context will be merged with the corresponding context in the project config:
  context:
    inputs:
    # local and secret are special cases that must be defined in the local config
    local:
     schemas:
       name1:
         type: string
         default
    secret:
     schemas:
       name1:
         type: string
         default
    environment:
    engine:
    external:
      name: # manifest to represent as a resource
        file:
        repository:
        instance: name or "*" # default is root
        schemas: # expected JSON schema for attributes

  spec:
    service_template:
      # <tosca service template>
    instances:
      name:
        template:
        implementations:
          action: implementationName
    implementations:
      name1:
        className
        version
        operation
        inputs:
  status:
    topology: repo:topology_template:revision
    inputs:
    outputs:
    readyState: #, etc.
    instances:
      name: # root resource is always named "root"
        template: repo:templateName:revision
        attributes:
          .interfaces:
            interfaceName: foo.bar.ClassName
        capabilities:
          - name:
            attributes:
            operatingStatus
            lastChange: changeId
        requirements:
         - name:
           operatingStatus
           lastChange
        readyState:
          effective:
          local:
          lastConfigChange: changeId
          lastStateChange: changeId
        priority
      resources:
        child1:
          # ...
  repositories
  changeLog: changes.yaml
  lastJob:
    changeId: 1
    previousId:
    startCommit:
    startTime:
    specDigest:
    lastChangeId:
    readyState:
      effective: error
      local: ok

.. jsonschema:: manifest-schema.json

changelog.yaml
~~~~~~~~~~~~~~

.. code-block:: YAML

  manifest: manifest.yaml
  changes:
    - changeId:
      previousId:
      startCommit:
      endCommit:
      startTime:
      specDigest:
      summary:
      readyState:
        effective: error
        local: ok
    - changeId: 2
      startTime
      target
      readyState
      priority
      resource
      config
      action
      implementation:
        type: resource | artifact | class
        key: repo:key#commitid | className:version
      inputs
      dependencies:
        - ref: ::resource1::key[~$val]
          expected: "value"
        - name: named1
          ref: .configurations::foo[.operational]
          required: true
          schema:
            type: array
      changes:
        resource1:
          .added: # set if added resource
          .status: # set when adding or removing
          foo: bar
        resource2:
          .spec:
          .status: absent
        resource3/child1: +%delete
      messages: []

.. jsonschema:: changelog-schema.json


unfurl.yaml
~~~~~~~~~~~

.. code-block:: YAML

  unfurl:
    version:

  contexts:
    defaults: # "defaults" are merged with optional contexts defined below
      # values are merged with the manifest's context:
      inputs:
      locals:
      secrets:
      environment:
      external:
    # user-defined contexts:
    # production:
    # staging:

  manifests:
    - file:
      repository:
      # default instance if there are multiple instances in that project
      # (only applicable when config is local to a project)
      default: True
      context: production # "defaults" context is used if not specified


.. jsonschema:: unfurl-schema.json

Sections
---------

.. jsonschema:: manifest-schema.json#/definitions/context

.. jsonschema:: manifest-schema.json#/definitions/instance

.. jsonschema:: manifest-schema.json#/definitions/external

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
