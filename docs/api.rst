Python API
===================================

.. contents::
    :depth: 2


.. _python:

API for writing configurators
-----------------------------

.. automodule:: unfurl.configurator
  :members: Configurator, TaskRequest, JobRequest, TaskView
  :undoc-members:

.. automodule:: unfurl.support
  :members: Status, NodeState, Priority
  :undoc-members:

.. automodule:: unfurl.result
  :members: ChangeRecord, ChangeAware

Runtime
~~~~~~~
.. automodule:: unfurl.runtime
  :members: Operational, OperationalInstance

APIs for controlling Unfurl
----------------------------

.. automodule:: unfurl.localenv
  :members: LocalEnv, Project
  :undoc-members:

.. automodule:: unfurl.job
  :members: runJob, JobOptions, ConfigChange, Job

.. automodule:: unfurl.plan
  :members: DeployPlan

.. automodule:: unfurl.init
  :members: clone, _createInClonedProject, createProjectRepo

Utility classes and functions
-----------------------------

.. automodule:: unfurl.eval
  :members: Ref, mapValue, evalRef, RefContext

.. automodule:: unfurl.util
  :members: UnfurlError, UnfurlTaskError, wrapSensitiveValue, isSensitive,
    sensitive_bytes, sensitive_str, sensitive_dict, sensitive_list,
    filterEnv, Generate
