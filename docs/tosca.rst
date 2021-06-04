.. _tosca:


TOSCA
=====

Introduction
~~~~~~~~~~~~

Topology and Orchestration Specification for Cloud Applications also abbreviated as TOSCA is an OASIS open standard that defines the interoperable description of services and applications hosted on the cloud and elsewhere. This includes their components, relationships, dependencies, requirements, and capabilities, thereby enabling portability and automated management across cloud providers regardless of underlying platform or infrastructure. 

TOSCA specification allows the user to define service template. The service generally refers either to an application or an application component. This may include a firewall, database config, etc or any other configuration following some set of rules. In order to successfully author a TOSCA template, it is important to understand the various terms used within the TOSCA specification and how are they interlinked.

.. seealso:: For more information, please refer to the :tosca_spec:`TOSCA Documentation <Generator>` 


Components of TOSCA Specification
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A Node Template and a Relationship Template are the building blocks of any TOSCA Specification.The diagram figure below provides a high level overview of how different components involved within the TOSCA specification.

.. https://app.diagrams.net/#G1rbe28yAmiULdCV2mtNJ_b0AWJFiG0iVi

.. image:: images/service_template.svg
   :width: 500px
   :align: center

Service Template
^^^^^^^^^^^^^^^^^

A Service Template consists of all the application or topology specific information and important configuration that is required to move the application from one cloud platform or infrastructure to another. For example, a service template can be created for a database server or a router. All this configuration can be defined in the form of schema. The figure shown above displays the components of a service template. In order to deploy a service template, the node and relationship information is needed to deploy a Service template. We will go through each of these in the sections below.

Example
-------

.. code::

 tosca_definitions_version: tosca_simple_unfurl_1_0_0
 metadata:
   template_name: Unfurl types
   template_author: onecommons.org
   template_version: 1.0.0
 repositories:
   docker_hub:
     url: https://registry.hub.docker.com/
     credential:
         user: a_user
         token: a_password
 imports:
 - namespace_prefix: k8s
   file: profiles/kubernetes/1.0/profile.yaml
 - namespace_prefix: o11n
   file: profiles/orchestration/1.0/profile.yaml
   repository: tosca-community-contributions
 node_types:
   # ... see the “node types” section below
 topology_template:
   # ... see the “topology_templates” section below
 node_templates:
   # ... see the “node_templates” section below

Node Type
^^^^^^^^^^

A Node Type includes all the operational details required to create, start, stop or delete a component or an event. All the operational possibilities, operations, properties, capabilities and requirements of resources or components which will be consumed during a deployment are defined within a node type. This information can then later be reused in a node or a relationship template.

Example
-------

.. code::

 node_types:

  # The Kubernetes profile comprises capability types, not node types
  # You need to create your own node type that is an assemblage of capabilities
  # In other words, the node is where we logically relate Kubernetes resources together
  Application:
    capabilities:
      # The Metadata capability will be shared with all resources
      # Only one should be used per node type
      metadata: k8s:Metadata
      # Other capabilities can be added to represent Kubernetes resources
      # (The same capability type can be used multiple times, e.g. two LoadBalancer)
      deployment: k8s:Deployment
      web: k8s:LoadBalancer
    interfaces:
      # Interfaces are used to achieve service modes
      # The name of the interface is used by default as the name of the mode
      # (Anything after "." in the name is ignored for this purpose)
      normal.1:
        type: k8s:ContainerCommand
      normal.2:
        type: o11n:Scriptlet


Relationship Type
^^^^^^^^^^^^^^^^^

A Relationship Template defines the interconnections that can take place between node types or node templates. In other words, it is a reuseable entity contaning definations of the relationships and the details on the nature of connectivity that can be used by an orchestration engine while working with various components of a service template.


Topology Template
^^^^^^^^^^^^^^^^^^

Topology Template refers to the topology model of a service. This model consists of node template and relationship template that are linked together to translate and structure the application in the form of TOSCA specification. A topoloigy template consists of reusable patterns of information defined in the node and relationship type.

Example
-------

.. code::

 topology_template:

  inputs:

    namespace:
      type: string
      default: workspace

  outputs:
    url:
      # Before a real attribute value arrives this will evaluate to "http://<unknown>:80"
      type: string
      value: { concat: [ http://, { get_attribute: [ hello-world, web, ingress, 0, ip ] }, ':80' ] }

    initialized:
      type: boolean
      value: false


Node Template
^^^^^^^^^^^^^

A Node Template defines an instance or a component of the application translated in the service template. Where a node type refers to the class or family of component at a high level that your resource belongs to, a node template makes use of the components defined in the node type and customizes the predefined properties and operations based upon the use case. The node templates also include operations such as deploy(), connect() to successfully and efficiently manage this component. Example: Shutting down an instance on the application server or hosting a container.

.. seealso:: To know more about these terminologies, refer to the :ref:`Glossary<glossary>` section.

The definitions of the application components within a node template have a potential to be reused later in the TOSCA specification by exporting the node dependencies using requirements and capabilities. The idea is to spcify the characteristics and the available functions of the components of a particular service in the form of nodes for future use.

Example
-------

.. code::

 node_templates:

    hello-world:
      type: Application
      capabilities:
        metadata:
          properties:
            # If "name" is not specified, the TOSCA node template name will be used
            # If "namespace" is not set, resources will be created in the same namespace as
            # the Turandot operator 
            namespace: { get_input: namespace }
            labels:
              app.kubernetes.io/name: hello-world
        deployment:
          properties:
            metadataNamePostfix: ''
            template:
              containers:
              - name: hello-world
                # You can, of course, specify any container image URL
                # (from the Docker Hub default or some other container image registry)
                # In this case, because the "image" is a ContainerImage, get_artifact will return
                # a URL for the Turandot inventory *after* the container image is pushed to it
                image: { get_artifact: [ SELF, image ] }
                imagePullPolicy: Always
        web:
          properties:
            ports:
            - { name: http, protocol: TCP, port: 80, targetPort: 8080 }
          attributes:
            # We're initializing this attribute to make sure the call to get_attribute in the ouput
            # won't fail before a real value arrives
            ingress:
            - ip: <unknown>
      interfaces:
        # The interfaces are executed in alphabetical order
        # The previous execution must succeed before moving on to the next
        normal.1:
          inputs:
            # The command is executed with the contents of the Clout in stdin
            # If the command has a non-empty stdout, it will be used to replace the current Clout
            # This combination allows the command to manipulate the Clout if desired
            command:
            - /tmp/configure.sh
            - $$nodeTemplate # argument beginning with "$$" will be replaced with local values
            # Artifacts are copied to the target container before execution
            artifacts:
            - configure # See below
        normal.2:
          inputs:
            scriptlet: hello-world.set-output
            arguments:
              name: initialized
              value: 'true'
      artifacts:
        # In this case all our artifacts are in the CSAR
        # But we can also use URLs to other locations
        image:
          # Container images will be published on the inventory before deployment 
          type: k8s:ContainerImage
          # Note that the container image tarball must be "portable"
          # (You can use the included "save-portable-container-image" script to create it)
          file: artifacts/images/hello-world.tar.gz
          properties:
            # The tag is required for publishing the image
            tag: hello-world
        configure:
          # The Executable type will set executable permissions on the file
          type: o11n:Executable
          file: artifacts/scripts/configure.sh
          deploy_path: /tmp/configure.sh



Relationship Template
^^^^^^^^^^^^^^^^^^^^^

A Relationship Template specifies the relationship between the components defined in the node templates and how the various nodes are connected to each other. Apart from the connections, relationship templates also include information regarding the dependencies and the order of the deployment that should be followed to instantiate a service template.

An important thing to notice here is, in a relationship, it is important for the node requirements of a component to match the capabilities of the node it is being linked to.


Putting Service Template Components Together
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. include:: examples/service-template.yaml
   :literal:

Extensions
~~~~~~~~~~

* add 'any' schema type for properties and attributes definitions
* 'additionalProperties' field in type metadata
* allow metadata field on inputs, outputs, artifacts, and repositories
* add "sensitive" property and datatype metadata field
* add "immutable" property metadata field
* add "environment" keyword to implementation definition
* add "eval" function
* add "type" in capability assignment
* allow workflow to be imported
* workflow "target" accepts type names
* groups can have other groups as members
* An operation's ``operation_host`` field can also be set to a node template's name.
* added ``OPERATION_HOST`` as a reserved function keyword.
* add "discover" and "default" directives
* add "default_for" keyword to relationship templates
* add "defaults" section to interface definitions
* add "types" section to the service template can contain any entity type definition.

Not yet implemented and non-conformance with the TOSCA 1.3 specification
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

* The ``get_operation_output`` function (use ``resultTemplate`` instead)
* "copy" keyword (use the ``dsl`` section or ``merge directives`` instead)
* ``get_artifact`` function (only implemented for artifacts that are container images)
* CSAR manifests and archives (implemented but untested)
* substitution mapping
* triggers
* notifications
* node_filters
* xml schema constraints


