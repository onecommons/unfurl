==================
Jobs and Workflows
==================

The core behavior of Unfurl is to run a :std:ref:`Job` that executes a `workflow` on a given topology instance.
There are two fundamental workflows ("normative workflows" in TOSCA terminology):
`deploy`, which installs the topology, and `undeploy`, which uninstalls it.

There are also :ref:`check` and  :std:ref:`discover` workflows which update the status of instances the based on their current live state.

Each of these workflows can be triggered with the equivalently named  :cli:`commands<unfurl-deploy-commands>`.

Job Lifecycle
==============

When a command that invokes a workflow is executed (`deploy`, `undeploy`, :ref:`check`,  :std:ref:`discover` and :ref:`run`)
a job is created and run. Running a job entails these steps:

1. YAML parsed and :ref:`merge directives<yaml_merge_directives>` are processed, including converting Python `DSL` code to TOSCA YAML.
2. Schema is validated and model instantiated. The command will exit if there are errors.
3. A plan is constructed based on the selected workflow and job options (use :cli:`unfurl plan<unfurl-plan>` command to preview) and the job begins.
4. For each operation a task is generated and the operation's :std:ref:`inputs` are lazily evaluated
   if referenced, including Unfurl expressions, TOSCA functions, and template strings.
5. Render the plan. The command will exit if there are unrecoverable errors. But render operations that depend on live attributes that will be deferred until those attributes are set when the job runs.
6. Execute the plan. You will be prompted with a plan summary to approve unless the `--approve` flag was used. As it runs, it tracks dependencies, and changes to resource attributes and status.
7. Re-execute steps 5 and 6 to render and run any deferred operations if needed. Repeat until all operations complete.
8. After the job completes, `ensemble.yaml` is updated with any changes to its instances status. It's ``jobs`` folder with have new `job.yaml` file and associated log files.
9. If the ``--commit`` flag was set, the ensemble's git repository will see a new commit,
   along with any other project repository that had changes to it.

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
:ref:`check` will be run first. Otherwise the workflow will be applied by executing one or more :ref:`operations<operation>` on a target instance.

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

Locking
==========

When a job is running Unfurl will lock the ensemble to prevent other instances of Unfurl from modifying the same ensemble.
There are two kinds of locks: a local lock that prevents access to the same local copy of an ensemble and a global lock which takes a `git lfs`_ lock to prevent access to the ensemble across remote servers.
Note that locks don't cause the job to block, instead the job will just abort when it starts. It is up to the user to re-run the job if aborts due to locking.

The local locking is always enabled but global remote lock need to be enabled via the ``lfs_lock`` section in your project's `environment` configuration. The following settings are available:


:lock: Whether to use git lfs when locking an ensemble, it can be set to one of:

    "no", don't try to lock (the default if missing)

    "try", take a lock if th e git lfs server is available

    "require", abort job if unable to take a git lfs lock

:url: The URL of the Git LFS server to use. If missing, the ensemble's git repository's "origin" remote will be used.

:name: Name of the lock file to use. Note that with git lfs, the file doesn't need to exist in in the git repository. If omitted, the local lock's file path will be used.
  By setting this you can set the scope to be coarser (or narrower) than each Individual ensemble as any ensemble using the same name will be locked.

  The following string substitutions are available to dynamically generate the name: ``$ensemble_uri``, ``$environment``, and ``$local_lock_path``. For example, setting a name like "group1/$environment" would prevent jobs from simultaneously running for ensemble with the lock name "group1" and in same the environment.

As these settings are `environment` settings, they will be merged with the current project, home project, and the ensemble's environment sections. Unlike most other environment settings, the ensemble's settings takes priority and overrides the project's settings.

.. _git lfs: https://git-lfs.com/