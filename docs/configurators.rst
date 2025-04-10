===============
Configurators
===============

Configurators is a software plugin that implements an operation and applies changes to instances. There are built-in configurators for shell scripts, Ansible playbooks, Terraform configurations, and Kubernetes resources or you can include your own as part of your blueprint.

To use a configurator, set it in the ``implementation`` field of an :std:ref:`Operation`
and set its inputs as documented below. Configurator names are case-sensitive;
if a configurator name isn't found it is treated as an external command.

If you set an external command line directly as the ``implementation``, Unfurl will choose the appropriate one to use.
If ``operation_host`` is local it will use the `Shell` configurator, if it is remote,
it will use the ``Ansible`` configurator and generate a playbook that invokes it on the remote machine:

.. tab-set-code::

  .. code-block:: YAML

    apiVersion: unfurl/v1alpha1
    kind: Ensemble
    spec:
      service_template:
        topology_template:
          node_templates:
            test_remote:
              type: tosca:Root
              interfaces:
                Standard:
                  configure: echo "abbreviated configuration"

  .. literalinclude:: ./examples/configurators-1.py
    :language: python


Configurators are fairly low-level. You can use "Installer" nodes that. Artifacts

Available configurators include:

.. contents::
   :local:
   :depth: 1

.. _ansible:

Ansible
========

The Ansible configurator executes the given playbook. You can access the same Unfurl filters and queries available in the Ensemble manifest from inside a playbook.

Example
-------

.. tab-set-code::

  .. code-block:: YAML

    apiVersion: unfurl/v1alpha1
    kind: Ensemble
    spec:
      service_template:
        topology_template:
          node_templates:
            test_remote:
              type: tosca:Root
              interfaces:
                Standard:
                  configure:
                    implementation: Ansible
                    inputs:
                      playbook:
                        # quote this yaml so its templates are not evaluated before we pass it to Ansible
                        q:
                          - set_fact:
                              fact1: "{{ '.name' | eval }}"
                          - name: Hello
                            command: echo "{{ fact1 }}"
                    outputs:
                      fact1:

  .. literalinclude:: ./examples/configurators-2.py
    :language: python

Inputs
------

  :playbook: (*required*) If string, treat as a file path to the Ansible playbook to run, otherwise treat as an inline playbook
  :inventory: If string, treat as a file path to an Ansible inventory file or directory, otherwise treat as in inline YAML inventory.
              If omitted, the inventory will be generated (see below)
  :arguments: A dictionary of variables that will be passed to the playbook as Ansible facts. See `arguments`
  :playbookArgs: A list of strings that will be passed to ``ansible-playbook`` as command-line arguments
  :resultTemplate: Same behavior as defined for `Shell` but will also include ``outputs`` as a variable.

Outputs
-------

Keys declared as outputs are used as the names of the Ansible facts to be extracted after the playbook completes.

``implementation`` key notes
-----------------------------

  :operation_host: If set, names the Ansible host.
  :environment: If set, environment directives will processed and passed to the playbook's ``environment``


Playbook processing
-------------------

The ``playbook`` input can be set to a full playbook or a list of tasks. If inventory is auto-generated and the "hosts" keyword is empty or missing from the playbook, "hosts" will be set to the host found in the auto-generated inventory, as described below.


Inventory
---------

If an inventory file isn't specified in ``inputs``, Unfurl will generate an Ansible inventory for the target host. The target host will be selected by searching for a node in the following order:

* The ``operation_host`` if explicitly set.
* The current target if it looks like a host (i.e. has an Ansible or SSH endpoint or is a Compute resource)
* Search the current target's ``hostedOn`` relationship for a node that looks like a host.
* Fallback to "localhost" with a local ansible connection.

The inventory facts for the selected host is built from the following sources:

* If host has an ``endpoint`` of  type ``unfurl.capabilities.Endpoint.SSH`` or ``unfurl.capabilities.Endpoint.Ansible`` use that capability's ``host``, ``port``, ``connection``, ``user``, and ``hostvars`` properties.
* If there is a relationship template or connection of
  type ``unfurl.relationships.ConnectsTo.Ansible`` that targets the endpoint, uses its ``credential`` and ``hostvars`` properties. (These can be set in the environment's :std:ref:`connections` section.)
* If the host is declared as a member of group of type ``unfurl.groups.AnsibleInventoryGroup`` in the service template,
  the group's name will be added as an ansible group along with the contents of the group's ``hostvars`` property.
* If ``ansible_host`` wasn't previously set, ``ansible_host`` will be set to the host's :ref:`public_ip<tosca_types>` or ``private_ip`` in that order if present, otherwise set it to ``localhost``.
* If the host is a Google compute instance the host name will be set to ``INSTANCE_NAME.ZONE.PROJECT`` e.g. ``instance-1.us-central1-a.purple-sanctum-25912``. This is for compatibility with the ``gcloud compute config-ssh`` command to enable Unfurl to use those credentials.

Execution environment
---------------------

  Unfurl runs Ansible in an environment isolated from your machine's Ansible installation
  and will not load the ansible configuration files in the standard locations.
  If you want to load an Ansible configuration file set the ``ANSIBLE_CONFIG`` environment variable.
  If you want Ansible to search standard locations set to an empty or invalid value like ``ANSIBLE_CONFIG=``.
  (See also the `Ansible Configurations Documentation`_)

  Note: Because Ansible is initialized at the beginning of execution,
  if the ``--no-runtime`` command option is used or if no runtime is available
  ``ANSIBLE_CONFIG`` will only be applied in the environment that executes Unfurl.
  It will not be applied if set via `environment` declaration.

  .. _Ansible Configurations Documentation: https://docs.ansible.com/ansible/latest/reference_appendices/config.html#the-configuration-file.


Cmd
====

The ``Cmd`` configurator executes a shell command using either the `shell` configurator
or the `ansible` configurator for remote execution if the ``operation_host`` is set to a remote node.
As described above, ``Cmd`` is the default configurator if none is specified.

Example
-------

In this example, ``operation_host`` is set to a remote instance so the command is executed remotely using Ansible.

.. tab-set-code::

  .. code-block:: YAML

    apiVersion: unfurl/v1alpha1
    kind: Ensemble
    spec:
      service_template:
        topology_template:
          node_templates:
            test_remote:
              type: tosca:Root
              interfaces:
                Standard:
                  configure:
                    implementation:
                      primary: Cmd
                      operation_host: staging.example.com
                    inputs:
                      cmd: echo "test"

  .. literalinclude:: ./examples/configurators-3.py
    :language: python

Delegate
========

The ``delegate`` configurator will delegate the current operation to the specified one.

Inputs
------

  :operation:  (*required*) The operation to delegate to, e.g. ``Standard.configure``
  :target: The name of the instance to delegate to. If omitted the current target will be used.
  :inputs: Inputs to pass to the operation. If omitted the current inputs will be used.
  :when: If set, only perform the delegated operation if its value evaluates to true.


.. _shell_configurator:

Shell
=====

The ``Shell`` configurator executes a shell command.

Inline shell script example
---------------------------

This example executes an inline shell script and uses the ``cwd`` and ``shell`` input options.

.. tab-set-code::

  .. code-block:: YAML

      apiVersion: unfurl/v1alpha1
      kind: Ensemble
      spec:
        service_template:
          topology_template:
            node_templates:
              shellscript-example:
                type: tosca:Root
                interfaces:
                  Standard:
                    configure:
                      implementation: |
                        if ! [ -x "$(command -v testvars)" ]; then
                          source testvars.sh
                        fi
                      inputs:
                          cwd: '{{ "project" | get_dir }}'
                          keeplines: true
                          # our script requires bash
                          shell: '{{ "bash" | which }}'

  .. literalinclude:: ./examples/configurators-4.py
    :language: python


Example with artifact
---------------------

Declaring an artifact of a type that is associated with the shell configurator
ensures Unfurl will install the artifact if necessary, before it runs the command.

.. tab-set-code::

  .. literalinclude:: ./examples/configurators-5.yaml
    :language: yaml

  .. literalinclude:: ./examples/configurators-5.py
    :language: python

Inputs
------

  :command: (*required*) The command to execute It can be either a string or a list of command arguments.
  :arguments: A map of arguments to pass to the command.
  :cwd:  Set the current working directory to execute the command in.
  :dryrun: During a during a dryrun job this will be either appended to the command line
           or replace the string ``%dryrun%`` if it appears in the command. (``%dryrun%`` is stripped out when running regular jobs.)
           If not set, the task will not be executed at all during a dry run job.
  :shell: If a string, the executable of the shell to execute the command in (e.g. ``/usr/bin/bash``).
          A boolean indicates whether the command if invoked through the default shell or not.
          If omitted, it will be set to true if ``command`` is a string or false if it is a list.
  :echo: A boolean that indicates whether or not should be standard output (and stderr)
         should be echoed to Unfurl's stdout while the command is being run.
         If omitted, true unless running with ``--quiet``.
         (Doesn't affect the capture of stdout and stderr.)
  :input: Optional string to pass as stdin.
  :keeplines: (*Default: false*) If true, preserve line breaks in the given command.
  :done: As as `done` defined by the `Template` configurator.
  :outputsTemplate: A `Jinja2 template<Ansible Jinja2 Templates>` or runtime expression that is processed after shell command completes, with same variables as ``resultTemplate``. The template should evaluate to a map to be used as the operation's outputs or null to skip.
  :resultTemplate: A `Jinja2 template<Ansible Jinja2 Templates>` or runtime expression that is processed after shell command completes, it will have the following template variables:

.. _resulttemplate:

Result template variables
-------------------------
All values will be either string or null unless otherwise noted.

  :success: *true* unless an error occurred or the returncode wasn't 0
  :cmd: (string) The command line that was executed
  :stdout:
  :stderr:
  :returncode: Integer (Null if the process didn't complete)
  :error: Set if an exception was raised
  :timeout: (Null unless a timeout occurred)

The processing the ``resultsTemplate`` is equivalent to passing its resulting YAML to `update_instances`.

Outputs
-------

No outputs are set unless ``outputsTemplate`` is present.

Template
=========

The template configurator lets you implement an operation entirely within the template.

Inputs
------

  :run:  Sets the ``result`` of this task.
  :dryrun: During a ``--dryrun`` job used instead of ``run``.
  :done:  If set, a map whose values passed as arguments to :py:meth:`unfurl.configurator.TaskView.done`
  :resultTemplate: A Jinja2 template or runtime expression that is processed with results of ``run`` as its variables.

Outputs
-------

Operation outputs are set from the `outputs<operation_outputs>` key on the ``done`` input if present.

.. _terraform:

Terraform
==========

The Terraform configurator will be invoked on any `node template` with the type :ref:`unfurl.nodes.Installer.Terraform<unfurl_types>`.
It can also be used to implement any operation regardless of the node type by setting the ``implementation`` to ``Terraform``.
It will invoke the appropriate terraform command (e.g "apply" or "destroy") based on the job's workflow.

Unless you set the ``stateLocation`` input parameter to "remote", the Terraform configurator manages the Terraform state file itself
and commits it to the ensemble's repository so you don't use Terraform's remote state -- it will be self-contained and sharable like the rest of the Ensemble.
Any sensitive state will be encrypted using Ansible Vault.

During a ``--dryrun`` job the configurator will validate and generate the Terraform plan but not execute it. You can override this behavior with the ``dryrun_mode`` input parameter and you can specify dummy outputs to use with the ``dryrun_outputs`` input parameter.

You can use the ``unfurl.nodes.Installer.Terraform`` node type with your node template to the avoid boilerplate and set the needed inputs.

Example
-------

.. tab-set-code::

  .. literalinclude:: ./examples/configurators-6.yaml
    :language: yaml

  .. literalinclude:: ./examples/configurators-6.py
    :language: python

Inputs
------

  :main: The contents of the root Terraform module or a path to a directory containing the Terraform configuration. If it is a directory path, the configurator will treat it as a local Terraform module. Otherwise, if ``main`` is a string it will be treated as HCL and if it is a map, it will be written out as JSON. (See the note below about HCL in YAML.) If omitted, the configurator will look in ``get_dir("spec.home")`` for the Terraform configuration.
  :tfvars: A map of Terraform variables to passed to the main Terraform module or a string equivalent to ".tfvars" file.
  :stateLocation: If set to "secrets" (the default) the Terraform state file will be encrypted and saved into the instance's "secrets" folder.
                  If set to "artifacts", it will be saved in the instance's "artifacts" folder with only sensitive values encrypted inline.
                  If set to "remote", Unfurl will not manage the Terraform state at all.
  :command: Path to the ``terraform`` executable. Default: "terraform"
  :dryrun_mode: How to run during a dry run job. If set to "plan" just generate the Terraform plan. If set to "real", run the task without any dry run logic. Default: "plan"
  :dryrun_outputs: During a dry run job, this map of outputs will be used simulate the task's outputs (otherwise outputs will be empty).
  :resultTemplate: A Jinja2 template or runtime expression that is processed with the Terraform state JSON file as its variables as well as the `resultTemplate` variables documented above for `Shell`.
     See the Terraform providers' schema documentation for details but top-level keys will include "resources" and "outputs".

Outputs
-------

Specifies which outputs defined by the Terraform module that will be set as the operation's outputs. If omitted and the Terraform configuration is specified inline, all of the Terraform outputs will be included. But if a Terraform configuration directory was specified instead, its outputs need to be declared here to be exposed.

``tfvar`` and ``tfoutput`` Metadata
-----------------------------------

You can automatically map properties and attributes to a Terraform variables and outputs by setting ``tfvar`` and ``tfoutput`` keys in the property and attribute metadata, respectively. For example:

.. tab-set-code::

  .. literalinclude:: ./examples/configurators-6b.yaml
    :language: yaml

  .. literalinclude:: ./examples/configurators-6b.py
    :language: python

Environment Variables
---------------------

If the ``TF_DATA_DIR`` environment variable is not defined it will be set to ``.terraform`` relative to the current working directory.

Note on HCL in YAML
-------------------

The json representation of the Terraform's HashiCorp Configuration Language (HCL) is quite readable when serialized as YAML:

Example 1: variable declaration

.. code-block::

  variable "example" {
    default = "hello"
  }

Becomes:

.. code-block:: YAML

  variable:
    example:
      default: hello

Example 2: Resource declaration

.. code-block::

  resource "aws_instance" "example" {
    instance_type = "t2.micro"
    ami           = "ami-abc123"
  }

becomes:

.. code-block:: YAML

  resource:
    aws_instance:
     example:
      instance_type: t2.micro
      ami:           ami-abc123

Example 3: Resource with multiple provisioners

.. code-block::

  resource "aws_instance" "example" {
    provisioner "local-exec" {
      command = "echo 'Hello World' >example.txt"
    }
    provisioner "file" {
      source      = "example.txt"
      destination = "/tmp/example.txt"
    }
    provisioner "remote-exec" {
      inline = [
        "sudo install-something -f /tmp/example.txt",
      ]
    }
  }

Multiple provisioners become a list:

.. code-block:: YAML

  resource:
    aws_instance:
      example:
        provisioner:
          - local-exec
              command: "echo 'Hello World' >example.txt"
          - file:
              source: example.txt
              destination: /tmp/example.txt
          - remote-exec:
              inline: ["sudo install-something -f /tmp/example.txt"]

You can convert HCL to JSON and YAML using tools like `hcl2json <https://github.com/tmccombs/hcl2json>`_ and `yq <https://github.com/mikefarah/yq/>`_, for example:

.. code-block:: shell

  hcl2json main.tf | yq -P -oyaml

Expressing terraform modules as YAML or JSON instead of HCL exposes the terraform in a structured way, making it easier to provide extensibility.
For example a subtype node template or artifact could add or update terraform resources defined on the base type: In the example below, a derived could redefines its base type's "main" property without have to replace the entire definition by using Ansible Jinja2's combine filter :

  main: "{{ combine('.super::main' | eval,  SELF.custom_changes, recursive=True, list_merge='append_rp') }}"

==================
Installers
==================

Installer node types already have operations defined.
You just need to import the service template containing the TOSCA type definitions and
declare node templates with the needed properties and operation inputs.

.. contents::
   :local:
   :depth: 1

.. _docker_configurator:

Docker
======

Required TOSCA import: ``configurators/templates/docker.yaml`` (in the ``unfurl`` repository)

unfurl.nodes.Container.Application.Docker
-----------------------------------------

TOSCA node type that represents a Docker container.

artifacts
~~~~~~~~~

  :image: (*required*) An artifact of type ``tosca.artifacts.Deployment.Image.Container.Docker``

By default, the configurator will assume the image is in `<https://registry.hub.docker.com>`_.
If the image is in a different registry you can declare it as a repository and have the ``image`` artifact reference that repository.

Inputs
-------

 :configuration:  A map that will included as parameters to Ansible's Docker container module
    They are enumerated `here <https://docs.ansible.com/ansible/latest/collections/community/docker/docker_container_module.html#ansible-collections-community-docker-docker-container-module#parameters>`_

Example
-------

.. tab-set-code::

  .. literalinclude:: ./examples/configurators-7.yaml
    :language: yaml

  .. literalinclude:: ./examples/configurators-7.py
    :language: python

DNS
====

The DNS installer support nearly all major DNS providers using `OctoDNS <https://github.com/octodns/octodns>`_.

Required TOSCA import: ``configurators/templates/dns.yaml`` (in the ``unfurl`` repository)

unfurl.nodes.DNSZone
---------------------

TOSCA node type that represents a DNS zone.

Properties
~~~~~~~~~~

  :name: (*required*) DNS hostname of the zone (should end with ".").
  :provider: (*required*) A map containing the `OctoDNS provider <https://github.com/octodns/octodns#supported-providers>`_ configuration
  :records: A map of DNS records to add to the zone (default: an empty map)
  :exclusive: Set to true if the zone is exclusively managed by this instance (removes unrecognized records) (default: false)

Attributes
~~~~~~~~~~

  :zone: A map containing the records found in the live zone
  :managed_records: A map containing the current records that are managed by this instance


unfurl.relationships.DNSRecords
-------------------------------

TOSCA relationship type to connect a DNS record to a DNS zone.
The DNS records specified here will be added, updated or removed from the zone when the relationship is established, changed or removed.

Properties
~~~~~~~~~~

  :records: (*required*) A map containing the DNS records to add to the zone.

Example
-------

.. tab-set-code::

  .. literalinclude:: ./examples/configurators-8.yaml
    :language: yaml

  .. literalinclude:: ./examples/configurators-8.py
    :language: python

.. _helm:

Helm
====

Requires Helm 3, which will be installed automatically if missing.

Required TOSCA import: ``configurators/templates/helm.yaml`` (in the ``unfurl`` repository)

unfurl.nodes.HelmRelease
------------------------

TOSCA type that represents a Helm release.
Deploying or discovering a Helm release will add to the ensemble any Kubernetes resources managed by that release.

Requirements
~~~~~~~~~~~~

  :host: A node template of type ``unfurl.nodes.K8sNamespace``
  :repository: A node template of type ``unfurl.nodes.HelmRepository``

Properties
~~~~~~~~~~

  :release_name: (*required*) The name of the helm release
  :chart: The name of the chart (default: the instance name)
  :chart_values: A map of chart values

Inputs
~~~~~~
  All operations can be passed the following input parameters:

  :flags: A list of flags to pass to the ``helm`` command

unfurl.nodes.HelmRepository
---------------------------

TOSCA node type that represents a Helm repository.

Properties
~~~~~~~~~~

  :name: The name of the repository (default: the instance name)
  :url: (*required*) The URL of the repository


.. _kubernetes:

Kubernetes
==========

Use these types to manage Kubernetes resources.

unfurl.nodes.K8sCluster
-----------------------

TOSCA type that represents a Kubernetes cluster. Its attributes are set by introspecting the current Kubernetes connection (``unfurl.relationships.ConnectsTo.K8sCluster``).
There are no default implementations defined for creating or destroying a cluster.

Attributes
~~~~~~~~~~

 :apiServer: The url used to connect to the cluster's api server.

unfurl.nodes.K8sNamespace
-------------------------

Represents a Kubernetes namespace. Destroying a namespace deletes any resources in it.
Derived from ``unfurl.nodes.K8sRawResource``.

Requirements
~~~~~~~~~~~~

  :host: A node template of type ``unfurl.nodes.K8sCluster``

Properties
~~~~~~~~~~

  :name: The name of the namespace.


unfurl.nodes.K8sResource
------------------------

Requirements
~~~~~~~~~~~~

  :host: A node template of type ``unfurl.nodes.K8sNamespace``

Properties
~~~~~~~~~~

  :definition: (map or string) The YAML definition for the Kubernetes resource.

Attributes
~~~~~~~~~~

  :apiResource: (map) The YAML representation for the resource as retrieved from the Kubernetes cluster.
  :name: (string) The Kubernetes name of the resource.

unfurl.nodes.K8sSecretResource
------------------------------

Represents a Kubernetes secret. Derived from ``unfurl.nodes.K8sResource``.

Requirements
~~~~~~~~~~~~

  :host: A node template of type ``unfurl.nodes.K8sNamespace``

Properties
~~~~~~~~~~

  :data: (map) Name/value pairs that define the secret. Values will be marked as sensitive.

Attributes
~~~~~~~~~~

  :apiResource: (map) The YAML representation for the resource as retrieved from the Kubernetes cluster.  Data values will be marked as sensitive.
  :name: (string) The Kubernetes name of the resource.

unfurl.nodes.K8sRawResource
---------------------------

A Kubernetes resource that isn't part of a namespace.

Requirements
~~~~~~~~~~~~

  :host: A node template of type ``unfurl.nodes.K8sCluster``

Properties
~~~~~~~~~~

  :definition: (map or string) The YAML definition for the Kubernetes resource.

Attributes
~~~~~~~~~~

  :apiResource: (map) The YAML representation for the resource as retrieved from the Kubernetes cluster.
  :name: (string) The Kubernetes name of the resource.

.. _sup:

Supervisor
==========

`Supervisor <http://supervisord.org>`_ is a light-weight process manager that is useful when you want to run local development instances of server applications.

Required TOSCA import: ``configurators/templates/supervisor.yaml`` (in the ``unfurl`` repository)

unfurl.nodes.Supervisor
-----------------------

TOSCA type that represents an instance of Supervisor process manager. Derived from ``tosca.nodes.SoftwareComponent``.

properties
~~~~~~~~~~

 :homeDir: (string) The location the Supervisor configuration directory (default: ``{get_dir: local}``)
 :confFile: (string) Name of the confiration file to create (default: ``supervisord.conf``)
 :conf: (string) The `supervisord configuration <http://supervisord.org/configuration.html>`_. A default one will be generated if omitted.

unfurl.nodes.ProcessController.Supervisor
-----------------------------------------

TOSCA type that represents a process ("program" in supervisord terminology) that is managed by a Supervisor instance. Derived from ``unfurl.nodes.ProcessController``.

.. _sup_requirements:

requirements
~~~~~~~~~~~~

  :host: A node template of type ``unfurl.nodes.Supervisor``.

properties
~~~~~~~~~~

  :name: (string) The name of this program.
  :program: (map) A map of `settings <http://supervisord.org/configuration.html#program-x-section-values>`_ for this program.


=============
Artifacts
=============

Instead of setting an operation's implementation to a configurator, you can set it to an `artifact`.
Using an artifact allows you to reuse an implementation with more than one operation. For example, you can create artifacts for specific Terraform modules, Ansible playbooks, or executables.

You define an ``execute`` operation on an artifact's type or template definition to specify the inputs and outputs that can be passed to the artifacts configurator. How the inputs and outputs are used depends on the artifact's type. For example, with a Terraform module artifact, its inputs will be used as the Terraform module's variables and its outputs the Terraform module's outputs. Or with a shell executable artifact, the inputs specify the command line arguments passed to the executable.

The example below declares an artifact that represents shell script and shows how an operation can invoke the artifact and pass values to artifact itself.

.. tab-set-code::

  .. literalinclude:: ./examples/artifact2.py
    :language: python

  .. literalinclude:: ./examples/artifact2.yaml
    :language: yaml

Arguments
=========

When an artifact is assigned as an operation's implementation, the operation `arguments` are passed to the artifact as the execute operation's inputs.

If ``arguments`` isn't explicitly declared as an operation input, one will be created:

* input defaults defined on the execution operation
* properties mapped to on the node or on the implementation artifact (see `Shared Properties`)
* operation inputs listed in "arguments" metadata key, if set. The Python DSL sets this based on the call to the ``execute`` method, as shown in the example above.
* if "arguments" metadata key is missing, operation inputs with the same name as above inputs or the execute operation's input definitions.

If an operation input with the same name as an execute input override any execute arguments and it is a validation error inputs doesn't meet the arguments input spec requirement (and the Python DSL will report a static type error).

Shared Properties
=================

In the Python DSL, TOSCA types can inherit from :py:class:`tosca.ToscaInputs` and  :py:class:`tosca.ToscaOutputs` classes using multiple inheritance and their fields will be inherited as TOSCA properties and attributes respectively. If an execute operation uses ToscaInputs as an argument in its method signature, any node or artifact that inherit that ToscaInputs class will have those properties passed as arguments. This way implementation definitions stay in sync with the nodes that use them.

In YAML, you can do the equivalent by adding a ``input_match`` metadata key to those properties to indicate they should be treated as arguments to operations. When invoking an operation, any property on the node or on the implementation artifact has that set will be added arguments. Its value can be a boolean or the name of an artifact to indicate that it should only be passed as arguments to operations that use that artifact. You can also control with properties are passed as arguments by adding an ``input_match`` metadata key to the artifact ``execute`` interface's metadata -- if set, only properties with matching ``input_match`` values will be set.  The YAML generated by the Python DSL uses that mechanism as the example below shows:

.. tab-set-code::

  .. literalinclude:: ./examples/shared-properties.py
    :language: python

  .. literalinclude:: ./examples/shared-properties.yaml
    :language: yaml

Abstract artifacts
==================

You can define abstract artifact types that just define the inputs and outputs it expects by defining an artifact type with an ``execute`` operation that doesn't have an implementation declared. Artifacts can implement that by, for example, by using multiple inheritance to inherit both the abstract artifact type and a concrete artifact type like ``unfurl.artifacts.TerraformModule``.

This way a node type can declare operations with abstract artifacts and node templates or a node subclass can set a concrete artifact without having to reimplement the operations that use it -- with the assurance that the static type checker will check that operation signatures are compatible.
