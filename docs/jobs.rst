==================
Jobs and Workflows
==================

The core behavior of Unfurl is to run a `job` that executes a `workflow` on a given topology instance.
There are two fundamental workflows ("normative workflows" in TOSCA terminology):
`deploy`, which installs the topology, and `undeploy`, which uninstalls it.

When a workflow job is run, it updates the status of its affected instances.
Each `instance` represented in a manifest has a status that indicates
its relationship to its specification:

Unknown
OK
Degraded
Error
Pending
Absent

There are also `check` and `discover` workflows which update the status of
instances the based on their current live state.
Users can also define custom workflows but they do not affect the change history of the topology.

When a workflow is applied to an instance it will be skipped if it already has
the desired status (either "OK" or "Absent"). If its status is `Unknown`,
`check` will be run first. Otherwise the workflow will be applied by executing one or more `operations` on a target instance.

If it succeeds, the target instance status will be set to either `OK` or `Absent`
for `deploy` and `undeploy` respectively. If it fails, the status will depend on whether the instance was modified by the operation.
If it has been, the status is set to `Error`; if the operation didn't report whether or not it was modified, it is set to `Unknown`. Otherwise, the status won't be changed.

\_ 1: or to Degraded, depending the priority of the task.

### Operations and tasks

The actual work is done by `operations` and as they are executed the `node state` of the target instance is updated.
Nodes states include: `initial`, `creating`, `created`, `configuring`, `configured`,
`starting`, `started`, `stopping`, `deleting`, and `error`.
See
`TOSCA 1.3, §3.4.1 <https://docs.oasis-open.org/tosca/TOSCA-Simple-Profile-YAML/v1.3/cos01/TOSCA-Simple-Profile-YAML-v1.3-cos01.html#_Toc454457724>`_ for a complete definitions

Each `task` in a `job` corresponds to an operation that was executed and is assigned a
`changeid`. Each task is recorded in the job's `changelog` as a `ConfigChange`,
which designed so that it can replayed to reproduce the instance.

Instances keep track of the last operation that was applied to it and also of the last
task that observed changes to the internal state of the instance (which may or may not be
reflected in attributes exposed in the topology model). Tracking internal state
is useful because dependent instances may need to know when it has changed and to know
if it is safe to delete an instance.

When status of an instance is saved in the manifest, the attributes described above
can be found in its `readyState`, for example:

.. code-block:: YAML

  readyState:
    local: ok # the explicit status of this instance
    effective: ok # its status with its dependencies' statuses considered
    state: started # operating state
  lastConfigChange: 99 # change id of the last ConfigChange that was applied
  lastStateChange: 80
