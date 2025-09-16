:orphan:

===============
Getting Started
===============

Once you've installed `Unfurl`, you are ready to configure it and create your first Unfurl project.

.. tip::
  Before diving in, it is recommended that you read this high-level overview of `How it works`_ section.

.. _create_servicetemplate:

3. Create a service template
============================

If you look at :ref:`ensemble_template.yaml<ensemble_yaml>`, you'll see that it contains a minimal template with one node template and one workflow.
Workflows are optional but defining one is the simplest way to get started,
because you just need to declare procedural steps instead of designing model of your topology.

Topology and Orchestration Specification for Cloud Applications (TOSCA) is an OASIS standard language to describe a topology of cloud based web services,
their components, relationships, and the processes that manage them.
The TOSCA standard includes specifications to describe processes that create or modify web services. You can read more about it on the OASIS website.

You can find examples

https://github.com/oasis-open/tosca-community-contributions/tree/master/examples/1.3/tutorial

.. tab-set-code::

  .. code-block:: YAML

    apiVersion: unfurl/v1.0.0
    kind: Ensemble
    spec:
        service_template:
          topology_template:
            node_templates:
              my_server:
                type: tosca.nodes.Compute
                capabilities:
                  # Host container properties
                  host:
                    properties:
                      num_cpus: 1
                      disk_size: 200GB
                      mem_size: 512MB
                  # Guest Operating System properties
                  os:
                    properties:
                      # host Operating System image properties
                      architecture: x86_64
                      type: linux
                      distribution: ubuntu
                      version: focal

  .. code-block:: python

    my_server = tosca.nodes.Compute(
    "my_server",
    host=tosca.capabilities.Compute(
        num_cpus=1,
        disk_size=200 * GB,
        mem_size=512 * MB,
      ),
    os=tosca.capabilities.OperatingSystem(
        architecture="x86_64",
        type="linux",
        distribution="ubuntu",
        version=tosca_version("focal"),
      ),
    )

.. _implement_operation:

4. Implementing an operation
============================

Of course, we don't have enough information "my_server" to actually create a compute instance -- it could be, for example, a physical machine, a virtual machine, a docker image or Kubernetes pod.
"my_server" are a set of abstract constraints that be applied to any number of

It is the implementation that create (or discover) instances that conforms to this specification.
Implementations are defined by specifying how to carry out operations that are applied to the node templates.
TOSCA defines a vocabulary of a few standard operations such as "create" or "delete" and you can define your own.
Their implementations can be a simple as the name of a shell script to invoke or yaml specification that is passed to a `configurator`,
which is Unfurl's plugin system for implementing operations.
Unfurl ships with several configurators, including ones for Ansible, Terraform and Kubernetes.

We can implement ``my_server`` in just few lines of YAML by Google Cloud Platform by calling the ``gcloud`` tool.
We'll start with "delete" to make the

.. tab-set-code::

  .. code-block:: YAML

    apiVersion: unfurl/v1.0.0
    kind: Ensemble
    spec:
      service_template:
      topology_template:
        node_templates:
          my_server:
            type: tosca.nodes.Compute
            # details omitted, see example above
            interfaces:
              Standard:
                delete:
                  implementation: gcloud compute instances delete {{ '.name' | eval }}
              # ... see example below for more operations

  .. code-block:: python

    @operation(name="delete")
    def my_server_delete(**kw):
        return unfurl.configurators.shell.ShellConfigurator(
            command=Eval("gcloud compute instances delete {{ '.name' | eval }}"),
        )

    my_server.delete = my_server_delete


Creates a little more verbose and illustrates how to pass input parameters and set attributes on the instance created from a template:

.. tab-set-code::

  .. code-block:: YAML

    apiVersion: unfurl/v1.0.0
    kind: Ensemble
    spec:
      service_template:
        topology_template:
          node_templates:
            my_server:
              type: tosca.nodes.Compute
              # details omitted, see example above
              interfaces:
                Standard:
                  delete:
                    implementation: gcloud compute instances delete {{ '.name' | eval }}
                  create:
                    implementation: |
                      gcloud compute instances create {{ '.name' | eval }}
                        --boot-disk-size={{ {"get_property": ["SELF", "host", "disk_size"]} | eval | regex_replace(" ") }}
                        --image=$(gcloud compute images list --filter=name:{{ {'get_property': ['SELF', 'os', 'distribution']} | eval }}
                              --filter=name:focal --limit=1 --uri)
                        --machine-type=e2-medium   > /dev/null
                      && gcloud compute instances describe {{ '.name' | eval }} --format=json
                    inputs:
                      resultTemplate:
                        # recursively merge the map with the yaml anchor "gcloudStatusMap"
                        +*gcloudStatusMap:
                        eval:
                          then:
                            attributes:
                              public_ip: "{{ result.networkInterfaces[0].accessConfigs[0].natIP }}"
                              private_ip: "{{ result.networkInterfaces[0].networkIP }}"
                              zone: "{{ result.zone | basename }}"
                              id:  "{{ result.selfLink }}"
                    # ...  see below

  .. code-block:: python

    @operation(name="delete")
    def my_server_delete(**kw):
        return unfurl.configurators.shell.ShellConfigurator(
            command=Eval("gcloud compute instances delete {{ '.name' | eval }}"),
        )


    @operation(name="create")
    def my_server_create(**kw):
        return unfurl.configurators.shell.ShellConfigurator(
            command=Eval(
                (
                    "gcloud compute instances create {{ '.name' | eval }}\n"
                    '--boot-disk-size={{ {"get_property": ["SELF", "host", "disk_size"]} | eval | '
                    'regex_replace(" ") }}\n'
                    "--image=$(gcloud compute images list --filter=name:{{ {'get_property': "
                    "['SELF', 'os', 'distribution']} | eval }}\n"
                    "      --filter=name:focal --limit=1 --uri)\n"
                    "--machine-type=e2-medium   > /dev/null\n"
                    "&& gcloud compute instances describe {{ '.name' | eval }} --format=json\n"
                )
            ),
            resultTemplate=Eval(
                {
                    "eval": {
                        "if": "$result"
                        "then": {
                            "attributes": {
                                "public_ip": "{{ "
                                "result.networkInterfaces[0].accessConfigs[0].natIP "
                                "}}",
                                "private_ip": "{{ "
                                "result.networkInterfaces[0].networkIP "
                                "}}",
                                "zone": "{{ result.zone | basename }}",
                                "id": "{{ result.selfLink }}",
                            }
                        }
                    },
                    "vars": {"result": "{%if success %}{{ stdout | from_json }}{% endif %}"}
                }
            ),
        )

    my_server.delete = my_server_delete
    my_server.create = my_server_create


This implementation calls ``gcloud compute instances create`` to create the instance
and then ``gcloud compute instances describe``. The ``resultTemplate`` parses that json and

One mysterious looking line is ``+*gcloudStatusMap:`` which is a `merge directive<YAML Merge directives>`
It's referencing a yaml map we haven't defined yet.
We'll see it when we finish off the implementation by defining the "check" operation:

.. tab-set-code::

  .. code-block:: YAML
    
    apiVersion: unfurl/v1.0.0
    kind: Ensemble
    spec:
      service_template:
        topology_template:
          node_templates:
            my_server:
              type: tosca.nodes.Compute
              # details omitted...
              interfaces:
                # other operations omitted, see example above
                Install:
                  check:
                    implementation: gcloud compute instances describe {{ '.name' | eval }}  --format=json
                    inputs:
                      resultTemplate:
                        +&: gcloudStatusMap
                        eval:
                          if: $result
                          then:
                            readyState:
                              state: "{{ {'PROVISIONING': 'creating', 'STAGING': 'starting',
                                        'RUNNING': 'started', 'REPAIRING' 'error,'
                                        'SUSPENDING': 'stopping',  'SUSPENDED': 'stopped',
                                        'STOPPING': 'deleting', 'TERMINATED': 'deleted'}[result.status] }}"
                              local: "{{ {'PROVISIONING': 'pending', 'STAGING': 'pending',
                                        'RUNNING': 'ok', 'REPAIRING' 'error,'
                                        'SUSPENDING': 'error',  'SUSPENDED': 'error',
                                        'STOPPING': 'absent', 'TERMINATED': 'absent'}[result.status] }}"
                        vars:
                          result: "{%if success %}{{ stdout | from_json }}{% endif %}"

  .. code-block:: python

    @operation(name="check")
    def my_server_check(**kw):
        return unfurl.configurators.shell.ShellConfigurator(
            command=Eval(
                "gcloud compute instances describe {{ '.name' | eval }}  --format=json"
            ),
            resultTemplate=Eval(
                {
                    "eval": {
                        "if": "$result",
                        "then": {
                            "readyState": {
                                "state": "{{ {'PROVISIONING': "
                                "'creating', 'STAGING': "
                                "'starting', 'RUNNING': "
                                "'started', 'REPAIRING' "
                                "'error,' 'SUSPENDING': "
                                "'stopping',  'SUSPENDED': "
                                "'stopped', 'STOPPING': "
                                "'deleting', 'TERMINATED': "
                                "'deleted'}[result.status] }}",
                                "local": "{{ {'PROVISIONING': 'pending', "
                                "'STAGING': 'pending', "
                                "'RUNNING': 'ok', 'REPAIRING' "
                                "'error,' 'SUSPENDING': "
                                "'error',  'SUSPENDED': "
                                "'error', 'STOPPING': 'absent', "
                                "'TERMINATED': "
                                "'absent'}[result.status] }}",
                            }
                        },
                    },
                    "vars": {
                        "result": "{%if success %}{{ stdout | from_json }}{% endif %}"
                    },
                }
            ),
        )


    my_server.check = my_server_check


The "check" operation is part of the ``Install`` interface, an Unfurl specific TOSCA extention.
It defines a "check" operation for checking the status of an existing interface; a "discover" operation for discovering pre-existing instances
and a "revert" operation for reverting changes made by Unfurl on a pre-existing resource.

The ``resultTemplate`` (shared with ``create``) maps Google Compute ["status" enumeration](https://cloud.google.com/compute/docs/instances/instance-life-cycle) to TOSCA's node state and to Unfurl's operation status.
We can see that it uses TOSCA's functions with Ansible's Jinja2 expressions and filters, glued together using Unfurl's expression syntax (``eval``)
https://docs.ansible.com/ansible/latest/user_guide/playbooks_filters.html

All together we have created 

.. tab-set-code::

  .. code-block:: YAML

    apiVersion: unfurl/v1.0.0
    kind: Ensemble
    spec:
      service_template:
        topology_template:
          node_templates:
            my_server:
              type: tosca.nodes.Compute
              capabilities:
                # Host container properties
                host:
                  properties:
                    num_cpus: 1
                    disk_size: 200GB
                    mem_size: 512MB
                # Guest Operating System properties
                os:
                  properties:
                    # host Operating System image properties
                    architecture: x86_64
                    type: linux
                    distribution: ubuntu
                    version: focal
              interfaces:
                Standard:
                  delete:
                    implementation: gcloud compute instances delete {{ '.name' | eval }}        
                  create:
                    implementation: |
                      gcloud compute instances create {{ '.name' | eval }}
                      --boot-disk-size={{ {"get_property": ["SELF", "host", "disk_size"]} | eval | regex_replace(" ") }}
                      --image=$(gcloud compute images list --filter=name:{{ {'get_property': ['SELF', 'os', 'distribution']} | eval }}
                            --filter=name:focal --limit=1 --uri)
                      --machine-type=e2-medium   > /dev/null
                      && gcloud compute instances describe {{ '.name' | eval }} --format=json
                    inputs:
                      resultTemplate:
                        # recursively merge the map with the yaml anchor "gcloudStatusMap"
                        # +*gcloudStatusMap:
                        eval: 
                          then:
                            attributes:
                              public_ip: "{{ result.networkInterfaces[0].accessConfigs[0].natIP }}"
                              private_ip: "{{ result.networkInterfaces[0].networkIP }}"
                              zone: "{{ result.zone | basename }}"
                              id:  "{{ result.selfLink }}"
                Install:
                  check:
                    implementation: gcloud compute instances describe {{ '.name' | eval }}  --format=json
                    inputs:
                      resultTemplate: &gcloudStatusMap
                        eval:
                          if: $result
                          then:
                            readyState:
                              state: "{{ {'PROVISIONING': 'creating', 'STAGING': 'starting',
                                        'RUNNING': 'started', 'REPAIRING' 'error,'
                                        'SUSPENDING': 'stopping',  'SUSPENDED': 'stopped',
                                        'STOPPING': 'deleting', 'TERMINATED': 'deleted'}[result.status] }}"
                              local: "{{ {'PROVISIONING': 'pending', 'STAGING': 'pending',
                                        'RUNNING': 'ok', 'REPAIRING' 'error,'
                                        'SUSPENDING': 'error',  'SUSPENDED': 'error',
                                        'STOPPING': 'absent', 'TERMINATED': 'absent'}[result.status] }}"
                        vars:
                          result: "{%if success %}{{ stdout | from_json }}{% endif %}"

  .. literalinclude:: ./examples/get-started.py
    :language: python


5 Activate your ensemble
========================

1. Run deploy
2. Commit your changes



.. _How it works: https://unfurl.run/howitworks.html