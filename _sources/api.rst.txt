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
