===============
Configurators
===============

Terraform
==========

The json representation of the terraform's HashiCorp Configuration Language (HCL) is quite readable when serialized as YAML

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
