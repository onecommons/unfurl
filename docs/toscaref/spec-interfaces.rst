.. _interfaces2:


Interfaces
==========

Interfaces provide a way to map logical tasks to executable operations.

Declaration
+++++++++++

Node Types and requirements Interface
--------------------------------------

.. code:: yaml

 node_types:
   some_type:
     interfaces:
       interface1:
         op1:
           ...
         op2:
           ...
       interface2:
         ...
 requirements:
   some_requirement:
     source_interfaces:
       interface1:
         ...
     target_interfaces:
       interface2:
         ...


Each interface declaration under the different ``interfaces``/``source_interfaces``/``target_interfaces`` sections is a dictionary of operations.

Operation Declaration in Node Types and requirements Interfaces
----------------------------------------------------------------

.. code:: yaml

 node_types:
   some_type:
     interfaces:
       interface1:
         op1:
           implementation: ...
           inputs:
             ...
           executor: ...
           max_retries: ...
           retry_interval: ...


Definition
++++++++++

.. list-table:: 
   :widths: 10 10 10 50
   :header-rows: 1

   * - Keyname
     - Required
     - Type
     - Description
   * - implementation 
     - yes
     - string
     - The script or plugin task name to execute.
   * - inputs
     - no
     - dict
     - Schema of inputs that will be passed to the implementation as kwargs.
   * - executor 
     - no
     - string
     - Valid values: central _deployment_agent, host_agent
   * - max_retries
     - no
     - number
     - Maximum number of retries for a task. -1 means infinite retries (Default: task_retries in manager tosca Cloudify Manager Type for remote workflows and task_retries workflow configuration for local workflows).
   * - retry_interval
     - no
     - number
     - Minimum wait time (in seconds) in between task retries (Default: task_retry_interval in manager tosca Manager Type for remote workflows and task_retry_interval workflow configuration for local workflows).


Simple Mapping
++++++++++++++

.. code:: yaml

 node_types:

   some_type:
     interfaces:
       interface1:
         op1: plugin_name.path.to.module.task

When mapping an operation to an implementation, if there is no need to
pass inputs or override the executor, the full mapping structure can be
avoided and the implementation can be written directly.

Operation Input Schema Definition
+++++++++++++++++++++++++++++++++

.. code:: yaml

 node_types:
   some_type:
     interfaces:
       interface1:
         op1:
           implementation: ...
           inputs:
             input1:
               description: ...
               type: ...
               default: ...
           executor: ...


.. list-table:: 
   :widths: 10 10 10 50
   :header-rows: 1

   * - Keyname
     - Required
     - Type
     - Description
   * - description 
     - no
     - string
     - Description for the input.
   * - type
     - no
     - string
     - stringInput type. Not specifying a data type means the type can be anything (also types not listed in the valid types). Valid types: string, integer, boolean
   * - default 
     - no
     - <any>
     - An optional default value for the input.


Node Templates Interface Definition
++++++++++++++++++++++++++++++++++++

.. code:: yaml

   some_node:
     interfaces:
       ...
     requirements:
       - type: ...
         target: ...
         source_interfaces:
           ...
         target_interfaces:
           ...


Operation Inputs in Node Templates Interfaces
---------------------------------------------

.. code:: yaml

 node_types:
   some_type:
     interfaces:
       interface1:
         op1:
           implementation: plugin_name.path.to.module.task
           inputs:
             input1:
               description: some mandatory input
             input2:
               description: some optional input with default
               default: 1000
           executor: ...

 node_templates:
   some_node:
     interfaces:
       interface1:
         op1:
           inputs:
             input1: mandatory_input_value
             input3: some_additional_input



When an operation in a node template interface is inherited from a node type or a requirement interface:

* All inputs that were declared in the operation inputs schema must be provided.
* Additional inputs, which were not specified in the operation inputs schema, may be passed as well.

Examples
++++++++

In the following examples, we will declare an interface which will allow us to:

* Configure a master deployment server using a plugin.
* Deploy code on the hosts using a plugin.
* Verify that the deployment succeeded using a shell script.
* Start the application after the deployment ended.

For the sake of simplicity, we will not refer to :ref:`requirements<requirements>` in these examples.

Configuring Interfaces in Node Types
------------------------------------

Configuring the master server:

.. code:: yaml

 plugins:
   deployer:
     executor: central_deployment_agent

 node_types:
   nodejs_app:
     derived_from: tosca.nodes.ApplicationModule
     properties:
       ...
     interfaces:
       my_deployment_interface:
         configure:
           implementation: deployer.config_in_master.configure

 node_templates:
   nodejs:
     type: nodejs_app

In this example, we’ve:

* Declared a ``deployer`` plugin which, `by default <#overriding-the-executor>`__, should execute its operations on the TOSCA manager.
* Declared a :ref:`node type<node_types>` with a ``my_deployment_interface`` interface that has a single ``configure`` operation which is mapped to the ``deployer.config_in_master.configure`` task.
* Declared a ``nodejs`` node template of type ``nodejs_app``.

Overriding the executor
-----------------------

In the above example we’ve declared an ``executor`` for our ``deployer`` plugin. TOSCA enables declaring an ``executor`` for a single operation thus overriding the previous declaration.

.. code:: yaml

 plugins:
   deployer:
     executor: central_deployment_agent

 node_types:
   nodejs_app:
     derived_from: tosca.nodes.ApplicationModule
     properties:
       ...
     interfaces:
       my_deployment_interface:
         configure:
           implementation: deployer.config_in_master.configure
         deploy:
           implementation: deployer.deploy_framework.deploy
           executor: host_agent

 node_templates:
   vm:
     type: tosca.openstack.nodes.Server
   nodejs:
     type: nodejs_app


Here we added a ``deploy`` operation to our ``my_deployment_interface``
interface. 

.. note:: Note that its ``executor`` attribute is configured to ``host_agent`` which means that even though the ``deployer`` plugin is configured to execute operations on the ``central_deployment_agent``, the ``deploy`` operation will be executed on hosts of the ``nodejs_app`` rather than the TOSCA manager.

Declaring an operation implementation within the node
-----------------------------------------------------

You can specify a full operation definition within the node’s interface under the node template itself.

.. code:: yaml

 plugins:
   deployer:
     executor: central_deployment_agent

 node_types:
   nodejs_app:
     derived_from: tosca.nodes.ApplicationModule
     properties:
       ...
     interfaces:
       my_deployment_interface:
         ...

 node_templates:
   vm:
     type: tosca.openstack.nodes.Server
   nodejs:
     type: nodejs_app
     interfaces:
       my_deployment_interface:
         ...
         start: scripts/start_app.sh


Let’s say that we use our ``my_deployment_interface`` on more than the ``nodejs`` node. While on all other nodes, a ``start`` operation is not mapped to anything, we’d like to have a ``start`` operation for the ``nodejs`` node specifically, which will run our application after it is deployed.

Here, we’ve declared a ``start`` operation and mapped it to execute a script specifically on the ``nodejs`` node.

This comes to show that you can define your interfaces either in ``node_types`` or in ``node_templates`` depending on whether you want to reuse the declared interfaces in diffrent nodes or declare them in specific nodes.

Operation Inputs
----------------

Operations can specify inputs that will be passed to the implementation.

.. code:: yaml

 plugins:
   deployer:
     executor: central_deployment_agent

 node_types:
   nodejs_app:
     derived_from: tosca.nodes.ApplicationModule
     properties:
       ...
     interfaces:
       my_deployment_interface:
         configure:
           ...
         deploy:
           implementation: deployer.deploy_framework.deploy
           executor: host_agent
           inputs:
             source:
               description: deployment source
               type: string
               default: git
         verify:
           implementation: scripts/deployment_verifier.py

 node_templates:
   vm:
     type: tosca.openstack.nodes.Server
   nodejs_app:
     type: tosca.nodes.WebServer
     interfaces:
       my_deployment_interface:
         ...
         start:
           implementation: scripts/start_app.sh
           inputs:
             app: my_web_app
             validate: true


Here, we added an input to the ``deploy`` operation under the ``my_deployment_interface`` interface in our ``nodejs_app`` node type and two inputs to the ``start`` operation in the ``nodejs`` node’s interface.


.. note:: Note that interface inputs are NOT the same type of objects as the inputs defined in the ``inputs`` section of the service template. Interface inputs are passed directly to a plugin’s operation (as \**kwargs to our ``deploy`` operation in the ``deployer`` plugin) or, in the case of our ``start`` operations, to the Script Plugin.

.. seealso:: For more information, refer to :tosca_spec2:`TOSCA Interface Section <_Toc50125307>`

