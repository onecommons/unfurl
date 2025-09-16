Python API
===================================

.. contents::
    :depth: 4


API for writing service templates
---------------------------------

See `here <dsl>` for an overview of the TOSCA Python DSL.

TOSCA Types
~~~~~~~~~~~~~~~~~~~~~~

The classes in this section are used to define TOSCA types corresponding to a TOSCA template (such as a node template or relationship template), including:

.. currentmodule:: tosca

.. autosummary::
    :nosignatures:

    Node
    Relationship
    CapabilityEntity
    DataEntity
    ArtifactEntity
    Group
    Policy

.. autoclass:: tosca.ToscaType

  .. automethod:: _template_init

  .. classmethod:: _class_init()

    A user-defined class method that is called when the class definition is being initialized, specifically before Python's `dataclass <https://docs.python.org/3/library/dataclasses.html>`_  machinery is invoked. Define this method in a subclass if you want to customize your class definition.

    Inside this method referencing a class fields will return a `FieldProjection` but this is hidden from the static type checker and IDE through `type punning <https://en.wikipedia.org/wiki/Type_punning>`_.

    You can set the default value of fields in your class definition to ``CONSTRAINED`` to indicate they will be configured in your ``_class_init`` method.

    :return type: None

    .. code-block:: python

      class Example(tosca.nodes.Root):

          # set default to CONSTRAINED to indicate a value will be assigned in _class_init
          host: tosca.nodes.Compute = tosca.CONSTRAINED

          @classmethod
          def _class_init(cls) -> None:
              # the proxy node template created here will be shared by all instances of Example unless one sets its own.
              cls.host = tosca.nodes.Compute()

              # Constrain the memory size of the host compute.
              # this will apply to all instances even if one sets its own Compute instance.
              # (The generated YAML for the host requirement on this node type will include a node_filter with an in_range property constraint).
              in_range(2 * gb, 20 * gb).apply_constraint(cls.host.mem_size)


  .. automethod:: __getattribute__

  .. automethod:: find_configured_by

  .. automethod:: find_hosted_on

  .. automethod:: from_owner

  .. automethod:: get_ref

  .. automethod:: has_default

  .. automethod:: patch

  .. automethod:: set_to_property_source

  .. automethod:: set_operation

  .. automethod:: set_inputs

  .. automethod:: clear_inputs

.. autoclass:: tosca.Node
  :show-inheritance:
  :members: _directives, _node_filter
  
  .. automethod:: find_required_by

  .. automethod:: find_all_required_by

  .. automethod:: substitute

.. autoclass:: tosca.Relationship
  :show-inheritance:

.. autoclass:: tosca.CapabilityEntity
   :show-inheritance:

.. autoclass:: tosca.DataEntity
  :show-inheritance:

.. autoclass:: tosca.ArtifactEntity
  :show-inheritance:

.. autoclass:: tosca.Group
  :show-inheritance:

.. autoclass:: tosca.Policy
  :show-inheritance:

TOSCA Field Specifiers
~~~~~~~~~~~~~~~~~~~~~~

The follow are functions that are used as field specified when declaring attributes on `TOSCA types`. Use these if you need to specify TOSCA specific information about the field or if the TOSCA field type can't be inferred from the Python's attribute's type. For example:

.. code-block:: python

    class MyNode(tosca.nodes.Root):
        a_tosca_property: str = Property(name="a-tosca-property", default=None, metadata={"foo": "bar"})


Note that these functions all take keyword-only parameters (this is needed for IDE integration).

.. automodule:: tosca
  :imported-members: true
  :members: Property, Attribute, Requirement, Capability, Artifact, operation, Computed

Other Tosca Objects
~~~~~~~~~~~~~~~~~~~

The following classes represent TOSCA entities that are not derived from `ToscaType` but correspond to TOSCA YAML constructs.

.. autoclass:: Interface
   :members: _type_name, _type_metadata, _interface_requirements

.. autoclass:: Repository
   :members:

.. autoclass:: ToscaInputs

.. autoclass:: ToscaOutputs

.. autoclass:: TopologyInputs

.. autoclass:: TopologyOutputs

.. autoclass:: ValueType
   :members: simple_tosca_type, simple_type

Property Constraints
~~~~~~~~~~~~~~~~~~~~

.. autoclass:: DataConstraint

.. autoclass:: equal

.. autoclass:: greater_than

.. autoclass:: less_than

.. autoclass:: greater_or_equal

.. autoclass:: less_or_equal

.. autoclass:: in_range

.. autoclass:: valid_values

.. autoclass:: length

.. autoclass:: min_length

.. autoclass:: max_length

.. autoclass:: pattern

.. autoclass:: schema

Other Module Items
~~~~~~~~~~~~~~~~~~~~~

.. autofunction:: Eval

.. autoclass:: EvalData
   :members:

.. autoclass:: Options

.. autoclass:: AttributeOptions

.. autoclass:: PropertyOptions

.. autoclass:: FieldProjection

.. autoclass:: StandardOperationsKeywords

.. autofunction:: set_operations

.. autoclass:: NodeTemplateDirective
   :show-inheritance: 
   :members:

.. autofunction:: find_node

.. autofunction:: find_relationship

.. autofunction:: jinja_template

.. autofunction:: patch_template

.. autofunction:: set_evaluation_mode

.. autofunction:: safe_mode

.. autofunction:: global_state_mode

.. autofunction:: global_state_context

.. autofunction:: reset_safe_mode


Scalars
~~~~~~~

.. automodule:: tosca.scalars
  :members:
  :private-members:
  :undoc-members:

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

Configurators
~~~~~~~~~~~~~

.. automodule:: unfurl.configurator
  :members: Configurator, TaskRequest, JobRequest, TaskView, ConfiguratorResult
  :undoc-members:

.. automodule:: unfurl.support
  :members: Status, NodeState, Priority, Reason
  :undoc-members:
  :show-inheritance:

.. automodule:: unfurl.result
  :members: ChangeRecord, ChangeAware

Project folders
~~~~~~~~~~~~~~~

.. automodule:: unfurl.projectpaths
  :members: WorkFolder
  :undoc-members:

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

Eval Expression API
~~~~~~~~~~~~~~~~~~~
.. automodule:: unfurl.eval
  :members: map_value, Ref, RefContext

Graphql module
~~~~~~~~~~~~~~
.. automodule:: unfurl.graphql
