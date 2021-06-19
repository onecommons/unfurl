==================
Jobs and Workflows
==================

The core behavior of Unfurl is to run a :std:ref:`Job` that executes a `workflow` on a given topology instance.
There are two fundamental workflows ("normative workflows" in TOSCA terminology):
`deploy`, which installs the topology, and `undeploy`, which uninstalls it.

There are also `check` and `discover` workflows which update the status of
instances the based on their current live state.
Users can also define custom workflows but they do not affect the change history of the topology.

Job Lifecycle
==============

When a command that invokes a workflow is executed (`deploy`, `undeploy`, `check`, `discover` and :ref:`run`)
a job is created and run. Running a job entails these steps:

1. YAML parsed and :ref:`merge directives<yaml_merge_directives>` are processed
2. Schema is validated and model instantiated. The command will exit if there are errors.
3. A plan is constructed based on the selected workflow and job options (use :cli:`unfurl plan<unfurl-plan>` command to preview) and the job begins.
4. For each operation a task is generated and the operation's :std:ref:`inputs` are lazily evaluated
   if referenced, including Unfurl expressions, TOSCA functions, and template strings.
5. After the job completes, `ensemble.yaml` is updated with any changes to its instances status.
   ``jobs.tsv`` will also be updated with line for each task run and a new `job.yaml` file is created in the ``jobs`` folder.
6. Depending on the commit options of the job, the ensemble's git repository will see a new commit,
   along with any other repository that had changes to it (e.g. files in the `spec` directory).

Operational status and state
=============================

When a workflow job is run, it updates the status of its affected instances.
Each `instance` represented in a manifest has a status that indicates
its relationship to its specification:

  :Unknown:  The operational state of the instance is unknown.
  :Pending:  Instance is being brought up or hasn't been created yet.
  :OK:       Instance is operational
  :Degraded: Instance is operational but in a degraded state.
  :Error:    Instance is not operational.
  :Absent:   Instance confirmed to not exist.

When a workflow is applied to an instance it will be skipped if it already has
the desired status (either "OK" or "Absent"). If its status is ``Unknown``,
`check` will be run first. Otherwise the workflow will be applied by executing one or more :ref:`operations<operation>` on a target instance.

If it succeeds, the target instance status will be set to either ``OK`` or ``Absent``
for `deploy` and `undeploy`, respectively.
If it fails, the status will depend on whether the instance was modified by the operation.
If it has been, the status is set to ``Error`` (or to ``Degraded`` the task was optional);
if the operation didn't report whether or not it was modified, it is set to ``Unknown``.
Otherwise, the status won't be changed.

Node state
~~~~~~~~~~

In addition, each instance has a ``node state`` which indicates where the instance is in
it deployment lifecycle. Node states are defined by TOSCA and include:
``initial``, ``creating``, ``created``, ``configuring``, ``configured``,
``starting``, ``started``, ``stopping``, ``deleting``, ``deleted``, and ``error``.

.. seealso::

 :tosca_spec:`TOSCA 1.3, §3.4.1 <_Toc454457724>` for a complete definitions

As :ref:`operations<operation>` are executed during a job, the target instance's `node state` is updated.

ChangeIds
==========

Each :ref:`task<tasks>` in a :std:ref:`Job` corresponds to an operation that was executed and is assigned a
`changeid`. Each task is recorded in the job's :ref:`changelog<job.yaml>` as a `ConfigChange`,
which designed so that it can replayed to reproduce the instance.

ChangeIds are unique within the lifespan of an ensemble and sortable using an encoded timestamp.
All copies of an ensemble maintain a consistent view of time to ensure proper serialization and easy of merging of changes
(using locking if necessary).

Instances keep track of the last operation that was applied to it and also of the last
task that observed changes to the internal state of the instance (which may or may not be
reflected in attributes exposed in the topology model). Tracking internal state
is useful because dependent instances may need to know when it has changed and to know
if it is safe to delete an instance.

When status of an instance is saved in the manifest, the attributes described above
can be found in its `readyState` section, for example:

.. code-block:: YAML

  readyState:
    local: ok # the explicit status of this instance
    effective: ok # its status with its dependencies' statuses considered
    state: started # node state
  lastConfigChange: A0AP4P9C0001 # change id of the last ConfigChange that was applied
  lastStateChange: A0DEVF0003 # change id of the last detected change to the instance
