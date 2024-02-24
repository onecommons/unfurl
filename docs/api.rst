Python API
===================================

.. contents::
    :depth: 3


API for writing service templates
---------------------------------

TOSCA Field Specifiers
~~~~~~~~~~~~~~~~~~~~~~

The follow are functions that are used as field specified when declaring attributes on TOSCA type. Use these if you need to specify TOSCA specific information about the field or if the TOSCA field type can't be inferred from the Python's attribute's type. For example:

.. code-block:: python

    class MyNodeType(tosca.nodes.Root):
        a_tosca_property: str = Property(name="a-tosca-property", default=None, metadata={"foo": "bar"})


Note that these functions all take keyword-only parameters (this is needed for IDE integration).


.. automodule:: tosca
  :imported-members: true
  :members: Property, Attribute, Requirement, Capability, Artifact, operation, Computed

TOSCA Types
~~~~~~~~~~~~~~~~~~~~~~

.. autoclass:: ToscaType

  .. automethod:: find_configured_by

  .. automethod:: find_hosted_on

  .. automethod:: set_to_property_source

.. autoclass:: NodeType
  
  .. automethod:: find_required_by

  .. automethod:: find_all_required_by

.. autoclass:: RelationshipType

.. autoclass:: CapabilityType

.. autoclass:: DataType

.. autoclass:: ArtifactType

.. autoclass:: InterfaceType

.. autoclass:: GroupType

.. autoclass:: PolicyType

Other
~~~~~~~~~~~~~~~~~~~~~~

.. autofunction:: Eval

.. autoclass:: DataConstraint

.. autoclass:: NodeTemplateDirective
   :members:

.. TODO:
  .. autoclass:: ToscaInputs
  .. autoclass:: ToscaOutputs

.. autofunction:: set_evaluation_mode

.. autofunction:: safe_mode

.. autofunction:: global_state_mode

.. autofunction:: global_state_context


Utility Functions
~~~~~~~~~~~~~~~~~

.. automodule:: unfurl.tosca_plugins.functions
   :members:

Eval Expression Functions
~~~~~~~~~~~~~~~~~~~~~~~~~

.. automodule:: unfurl.tosca_plugins.expr
  :members:
  :undoc-members:

API for writing configurators
-----------------------------

.. automodule:: unfurl.configurator
  :members: Configurator, TaskRequest, JobRequest, TaskView, ConfiguratorResult
  :undoc-members:

.. automodule:: unfurl.support
  :members: Status, NodeState, Priority, Reason
  :undoc-members:

.. automodule:: unfurl.result
  :members: ChangeRecord, ChangeAware

Runtime module
~~~~~~~~~~~~~~
.. automodule:: unfurl.runtime
  :members: Operational, OperationalInstance

APIs for controlling Unfurl
----------------------------

Localenv module
~~~~~~~~~~~~~~~
.. automodule:: unfurl.localenv
  :members: LocalEnv, Project
  :undoc-members:

Job module
~~~~~~~~~~~~~~
.. automodule:: unfurl.job
  :members: run_job, JobOptions, ConfigChange, Job

Init module
~~~~~~~~~~~
.. automodule:: unfurl.init
  :members: clone

Utility classes and functions
-----------------------------

.. automodule:: unfurl.logs
  :members: sensitive

.. automodule:: unfurl.util
  :members: UnfurlError, UnfurlTaskError, wrap_sensitive_value, is_sensitive,
    sensitive_bytes, sensitive_str, sensitive_dict, sensitive_list,
    filter_env

Eval module
~~~~~~~~~~~~~~
.. automodule:: unfurl.eval
  :members: Ref, map_value, eval_ref, RefContext

Graphql module
~~~~~~~~~~~~~~
.. automodule:: unfurl.graphql
