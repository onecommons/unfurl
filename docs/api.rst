Python API
===================================

.. contents::
    :depth: 2


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
  :members: run_job, JobOptions, ConfigChange, Job

.. automodule:: unfurl.plan
  :members: DeployPlan

.. automodule:: unfurl.init
  :members: clone, _create_in_cloned_project

Utility classes and functions
-----------------------------

.. automodule:: unfurl.eval
  :members: Ref, map_value, eval_ref, RefContext

.. automodule:: unfurl
  :members: sensitive

.. automodule:: unfurl.util
  :members: UnfurlError, UnfurlTaskError, wrap_sensitive_value, is_sensitive,
    sensitive_bytes, sensitive_str, sensitive_dict, sensitive_list,
    filter_env, Generate
