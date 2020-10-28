===============
Configurators
===============

Ansible
========

Example

Installs and supports Ansible 2.9
https://docs.ansible.com/ansible/2.9/index.html

https://docs.ansible.com/ansible/2.9/modules/modules_by_category.html

- doc ANSIBLE_CONFIG var
- use public_ip or private_ip or name as ansible host in inventory/gcp_compute.py
- use case for ansible connecting to gcp servers: need to support dynamic inventory for ssh connection info
- # INSTANCE_NAME.ZONE.PROJECT e.g. instance-1.us-central1-a.glowing-sanctum-287822
  # gcloud compute config-ssh --quiet updates ~/.ssh/config
- unfurl filters and query are available inside playbooks
- unfurl.groups.AnsibleInventoryGroup

Inputs
------

  :playbook: (*required*) If string, treat as a file path to the Ansible playbook to run, otherwise treat as an inline playbook
  :inventory: If string, treat as a file path to an Ansible inventory file or directory, otherwise treat as in inline inventory.
              If omitted, the inventory host will be set to the ``operation_host``
  :extraVars: A dictionary of variables that will be passed to the playbook as Ansible facts
  :playbookArgs: A list of strings that will be passed to ``ansible-playbook`` as command-line arguments
  :resultTemplate: As `shell` below, with the additional of the variable named ``outputs`` containing the results from

Other ``implementation`` keys
-----------------------------

  :operation_host: Defaults to ORCHESTRATOR (localhost). Set to HOST to have Ansible connect to the Compute instance is being targeted by this task.
  :environment: If set, environment directives will processed and passed to the playbooks ``environment``
  :outputs: Keys are the names of Ansible facts to by extracted after the playbook completes. Value are currently ignored

Delegate
========

The ``delegate`` configurator will delegate the current operation to the specified one.

Inputs
------

  :operation:  (*required*) The operation to delegate to.
  :target: The name of the instance to delegate to. If omitted the current target will be used.


Docker
======

imports:
  - repository: unfurl
    file: configurators/docker-template.yaml

unfurl.nodes.Container.Application.Docker

artifacts:
  image:
    type: tosca.artifacts.Deployment.Image.Container.Docker
    file: busybox

By default, the configurator will assume the image is in ``https://registry.hub.docker.com``.
If the image is in a different registry you can declare it as a repository and have the ``image`` artifact reference that repository.

Inputs
-------

 :configuration: https://docs.ansible.com/ansible/latest/modules/docker_container_module.html#docker-container-module


Helm
====

release_name: (*required*)
chart
chart_values
flags

Kubernetes
==========

Shell
=====

Inputs
------

  :command: (*required*) The command. It can be either a list or a string.
  :cwd:
  :dryrun: A string that will be either appended to the command line during a ``--dryrun`` job or replace the string ``%dryrun^`` if appears in the command line.
           If not set, the task will not be executed at all during a dry run.
  :shell: If a string the executable of the shell to execute the command in (e.g. ``/usr/bin/bash``).
          A boolean indicates whether the command if invoked through the default shell or not.
          If omitted, it will be set to true if `command` is a string or false if it is a list.
  :echo: (*Default: true*) Whether or not should be standard output (and stderr)
         should be echod to Unfurl's stdout while the command is being run.
         (Doesn't affect the capture of stdout and stderr.)
  :keeplines:
  :done: As as `done` defined by the `Template` configurator.
  :resultTemplate: A Jinja2 template that is processed after shell command completes

Result template variables
-------------------------
All values will be either string or null unless otherwise noted

  :success: *true* unless an error occurred or the returncode wasn't 0
  :cmd: (string) The command line that was executed
  :stdout:
  :stderr:
  :returncode: Integer (None if the process didn't complete)
  :error: Set if an exception was raised
  :timeout: (Null unless a timeout occurred)

Supervisor
==========

Supervisor is a light-weight process manager that is useful when you want to run a local development instance of server applications.
The supervisor configurator will.

Template
=========

The template configurator lets you implement an operation entirely within the template.

Inputs
------

  :run:  Sets the ``result`` of this task.
  :dryrun: During a ``--dryrun`` job used instead of ``run``.
  :done:  If set, a map whose values passed as arguments to :py:meth:`unfurl.configurator.TaskView.done`
  :resultTemplate: A Jinja2 template that is processed with results of ``run`` as its variables.

Terraform
==========

The Terraform configurator will be invoked on any `node template` with the type `unfurl.nodes.Installer.Terraform`.
It can also be used to implement any operation regardless of the node type by setting the `implementation` to `unfurl.configurators.terraform.TerraformConfigurator`.
It will invoke the appropriate terraform command (e.g "apply" or "destroy") based on the job's workflow.

The Terraform configurator manages the Terraform state file itself
and commits it to the ensemble's repository so you don't have use Terraform's remote state -- it will be self-contained and sharable like the rest of the Ensemble.
Any data marked sensitive will be encrypted using Ansible Vault.

Inputs
------

  :main: If present, its value will be used to generate `main.tf`.
         If it's a string it will be treated as HCL, otherwise it will be written out as JSON.
  :tfvars: A map of Terraform variables to passed to the main Terraform module.
  :dir:  The directory to execute Terraform in. Default: equivalent to get_dir("spec.home")
  :command: Path to the ``terraform`` executable. Default: "terraform"
  :resultTemplate: A map that corresponds to the Terraform state JSON file,
    See the Terraform providers' schema documentation for details but top-level keys will include "resources" and "outputs".

Other ``implementation`` keys
-----------------------------

  :environment: This will set the environment variables exposed to Terraform.
  :outputs: The outputs defined by the Terraform module will be set as the operation's outputs.

Environment Variables
---------------------

If the ``TF_DATA_DIR`` environment variable is not defined it will be set to `.terraform` relative to the current working directory.

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
