.. _configurators:

===============
Configurators
===============

To use a configurator, set it as the ``implementation`` field of an `operation`
and set its inputs as documented below. Configurator names are case-sensitive;
if a configurator name isn't found it is treated as an external command.

If you set an external command line directly as the ``implementation``, Unfurl will choose the appropriate one to use.
If ``operation_host`` is local it will use the `Shell` configurator, if it is remote,
it will use the ``Ansible`` configurator and generate a playbook that invokes it on the remote machine.

.. contents::
   :local:
   :depth: 1

.. _ansible:

Ansible
========

The Ansible configurator executes the given playbook.

Unfurl uses `Ansible 2.9 <https://docs.ansible.com/ansible/2.9/index.html>`_  as a library.
These `Ansible modules <https://docs.ansible.com/ansible/2.9/modules/modules_by_category.html>`_ are available by default.

You can access the same Unfurl filters and queries available in the Ensemble manifest from inside a playbook.

Inputs
------

  :playbook: (*required*) If string, treat as a file path to the Ansible playbook to run, otherwise treat as an inline playbook
  :inventory: If string, treat as a file path to an Ansible inventory file or directory, otherwise treat as in inline YAML inventory.
              If omitted, the inventory host will be set to the ``operation_host``
  :extraVars: A dictionary of variables that will be passed to the playbook as Ansible facts
  :playbookArgs: A list of strings that will be passed to ``ansible-playbook`` as command-line arguments
  :resultTemplate: Same behavior as defined for `Shell` but will also include ``outputs`` as a variable.

Other ``implementation`` keys
-----------------------------

  :operation_host: Defaults to ORCHESTRATOR (localhost). Set to HOST to have Ansible connect to the Compute instance that is hosting the instance targeted by this task.
  :environment: If set, environment directives will processed and passed to the playbooks ``environment``
  :outputs: Keys are the names of Ansible facts to be extracted after the playbook completes. Value are currently ignored.

Execution environment
---------------------

  Unfurl runs Ansible in an environment isolated from your machine's Ansible installation
  and will not load the ansible configuration files in the standard locations.
  If you want to load an Ansible configuration file set the ``ANSIBLE_CONFIG`` environment variable.
  If you want Ansible to search standard locations set to an empty or invalid value like ``ANSIBLE_CONFIG=``.
  (See also the `Ansible Configurations Documentation`_)

  Note: Because Ansible is initialized at the beginning of execution,
  if the ``no-runtime`` command option is used or if no runtime is available
  ``ANSIBLE_CONFIG`` will only be applied if in the environment that executes Unfurl.
  It will not be applied if set in a `context`.

  .. _Ansible Configurations Documentation: https://docs.ansible.com/ansible/latest/reference_appendices/config.html#the-configuration-file.

Inventory
---------

If an inventory file isn't specified in ``inputs``, Unfurl will generate an Ansible inventory using the ``operation_host``
as the host. The inventory will include groups and variables derived from the following sources:

* If the ``operation_host`` has an ``endpoint`` of  type ``unfurl.capabilities.Endpoint.SSH`` or ``unfurl.capabilities.Endpoint.Ansible``
  use that capabilities ``host``, ``port``, ``connection``, ``user``, ``credential``, and ``hostvars`` properties.
* If the ``operation_host`` has relationship template or connection to the target instance of
  type ``unfurl.relationships.ConnectsTo.Ansible`` uses its ``connection`` and ``hostvars`` properties.
* If the operation_host is declared as a member of group of type ``unfurl.groups.AnsibleInventoryGroup`` in the service template,
  the group's name will be added as an ansible group along with the contents of the group's ``hostvars`` property.
* If ``ansible_host`` wasn't previously set, the host name will be set to the operation_host's :ref:`public_ip<tosca_types>` or ``private_ip`` in that order, otherwise set it to ``localhost``.
* If the host is a Google compute instance the host name will be set to ``INSTANCE_NAME.ZONE.PROJECT`` e.g. ``instance-1.us-central1-a.purple-sanctum-25912``. This is for compatibility with the ``gcloud compute config-ssh`` command to enable Unfurl to use those credentials.

Delegate
========

The ``delegate`` configurator will delegate the current operation to the specified one.

Inputs
------

  :operation:  (*required*) The operation to delegate to.
  :target: The name of the instance to delegate to. If omitted the current target will be used.

.. _shell:

Shell
=====

The ``Shell`` configurator executes a shell command.

Inputs
------

  :command: (*required*) The command. It can be either a string or a list of command arguments.
  :cwd:  Set the current working directory to execute the command in.
  :dryrun: During a during a dryrun job this will be either appended to the command line
           or replace the string ``%dryrun%`` if it appears in the command. (``%dryrun%`` is stripped out when running regular jobs.)
           If it is not set, the task will not be executed at all during a dry run job.
  :shell: If a string, the executable of the shell to execute the command in (e.g. ``/usr/bin/bash``).
          A boolean indicates whether the command if invoked through the default shell or not.
          If omitted, it will be set to true if ``command`` is a string or false if it is a list.
  :echo: (*Default: true*) Whether or not should be standard output (and stderr)
         should be echod to Unfurl's stdout while the command is being run.
         (Doesn't affect the capture of stdout and stderr.)
  :keeplines:
  :done: As as `done` defined by the `Template` configurator.
  :resultTemplate: A Jinja2 template that is processed after shell command completes, it will have the following template variables:

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

Template
=========

The template configurator lets you implement an operation entirely within the template.

Inputs
------

  :run:  Sets the ``result`` of this task.
  :dryrun: During a ``--dryrun`` job used instead of ``run``.
  :done:  If set, a map whose values passed as arguments to :py:meth:`unfurl.configurator.TaskView.done`
  :resultTemplate: A Jinja2 template that is processed with results of ``run`` as its variables.

.. _terraform:

Terraform
==========

The Terraform configurator will be invoked on any `node template` with the type :ref:`unfurl.nodes.Installer.Terraform<unfurl_types>`.
It can also be used to implement any operation regardless of the node type by setting the `implementation` to ``Terraform``.
It will invoke the appropriate terraform command (e.g "apply" or "destroy") based on the job's workflow.

The Terraform configurator manages the Terraform state file itself
and commits it to the ensemble's repository so you don't use Terraform's remote state -- it will be self-contained and sharable like the rest of the Ensemble.
Any data marked sensitive will be encrypted using Ansible Vault.

You can use the ``unfurl.nodes.Installer.Terraform`` node type with your node template to the avoid boilerplate and set the needed inputs.

Inputs
------

  :main: The contents of the root Terraform module or a path to a directory containing the Terraform configuration. If it is a directory path, the configurator will treat it as a local Terraform module. Otherwise, if ``main`` is a string it will be treated as HCL and if it is a map, it will be written out as JSON. (See the note below about HCL in YAML.) If omitted, the configurator will look in ``get_dir("spec.home")`` for the Terraform configuration.
  :tfvars: A map of Terraform variables to passed to the main Terraform module or a string equivalent to ".tfvars" file.
  :workdir:  String indicating the project location to execute Terraform in (see `get_dir`). Default: "home"
  :command: Path to the ``terraform`` executable. Default: "terraform"
  :resultTemplate: A map that corresponds to the Terraform state JSON file,
    See the Terraform providers' schema documentation for details but top-level keys will include "resources" and "outputs".

Other ``implementation`` keys
-----------------------------

  :environment: This will set the environment variables exposed to Terraform.
  :outputs: Specifies which outputs defined by the Terraform module that will be set as the operation's outputs. If omitted and the Terraform configuration is specified inline, all of the Terraform outputs will be included. But if a Terraform configuration directory was specified instead, its outputs need to be declared here to be exposed.

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

==================
Installers
==================

Installation types already have operations defined.
You just need to import the service template containing the TOSCA type definitions and
declare node templates with the needed properties and operation inputs.

.. contents::
   :local:
   :depth: 1

.. _docker:

Docker
======

Required TOSCA import: ``configurators/docker-template.yaml`` (in the ``unfurl`` repository)

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
    They are enumerated `here <https://docs.ansible.com/ansible/latest/modules/docker_container_module.html#docker-container-module>`_

.. code-block:: YAML

  node_templates:
    hello-world-container:
      type: unfurl.nodes.Container.Application.Docker
      requirements:
        - host: compute
      artifacts:
        image:
          type: tosca.artifacts.Deployment.Image.Container.Docker
          file: busybox
      interfaces:
        Standard:
          inputs:
            configuration:
              command: ["echo", "hello world"]
              detach:  no
              output_logs: yes

.. _helm:

Helm
====

Requires Helm 3, which will be installed automatically if the default ``.unfurl_home`` ensemble is deployed.

Required TOSCA import: ``configurators/helm-template.yaml`` (in the ``unfurl`` repository)

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

Required TOSCA import: ``configurators/supervisor-template.yaml`` (in the ``unfurl`` repository)

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

requirements
~~~~~~~~~~~~

  :host: A node template of type ``unfurl.nodes.Supervisor``.

properties
~~~~~~~~~~

  :name: (string) The name of this program.
  :program: (map) A map of `settings <http://supervisord.org/configuration.html#program-x-section-values>`_ for this program.
