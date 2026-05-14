import traceback
import unittest
import os
import pprint
import logging
import io
import time
import pytest
from unfurl.localenv import LocalEnv
from unfurl.util import UnfurlValidationError
from unfurl.yamlmanifest import YamlManifest
from unfurl.job import Job, Runner, JobOptions
from unfurl.configurators import TemplateConfigurator
from unfurl.projectpaths import FilePath
from unfurl.result import serialize_value

_manifestTemplate = """
apiVersion: unfurl/v1.0.0
kind: Ensemble
spec:
  service_template:
    types:
      # declare the attribute as a property in a base type to test that
      # a property can be "promoted" to an attribute in a derived class
      nodes.Base:
        derived_from: tosca.nodes.Root
        properties:
          attr:
            type: string
            default: Default

      nodes.Test:
        derived_from: nodes.Base
        attributes:
          attr:
            type: string
        interfaces:
         Standard:
          operations:
            configure:
              implementation: Template
              inputs:
                run:
                  somevalue: true
                done:
                  status: %s

    topology_template:
      node_templates:
        nodeA:
          type: tosca:Root
          interfaces:
           Standard:
            operations:
              configure:
                implementation: Template
                inputs:
                      # an undeclared property
                  run: "{{ NODES.nodeC.prop }}"
                  done:
                    status: ok

        nodeB:
          type: tosca:Root
          interfaces:
           Standard:
            operations:
              configure:
                implementation: Template
                inputs:
                  # a property given attribute status by derived type
                  run: "{{ NODES.nodeC.attr }}"
                  done:
                    status: ok

        nodeC:
          type: nodes.Test
          properties:
            prop: static
          # attributes: # XXX this is currently ignored!
          #   attr: live
"""

manifestContent = _manifestTemplate % "ok"
manifestErrorContent = _manifestTemplate % "error"

reorderManifest = (
    manifestContent
    + """
        nodeD:
          type: nodes.Test
          requirements:
          - host: nodeB
"""
)

static_dep_manifest = """
apiVersion: unfurl/v1.0.0
kind: Ensemble
spec:
  service_template:
    node_types:
      my_host_type:
        derived_from: tosca:Root
        capabilities:
          host:
            type: tosca.capabilities.Compute

    topology_template:
      node_templates:
        my_host:
          type: my_host_type
          interfaces:
            Standard:
              operations:
                configure:
                  implementation: exit 1

        my_node:
          type: tosca.nodes.SoftwareComponent
          requirements:
          - host: my_host
          interfaces:
            Standard:
              operations:
                create:
                  implementation: echo "Here"
"""


def test_digests(caplog):
    path = __file__ + "/../examples/digest-ensemble.yaml"
    manifest = LocalEnv(path, "").get_manifest()
    runner = Runner(manifest)
    output = io.StringIO()  # so we don't save the file
    job = runner.run(JobOptions(startTime=1, out=output))  # deploy
    assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
    # print(job.out.getvalue())
    digestKeys = job.manifest.manifest.config["changes"][0]["digestKeys"]
    assert (
        digestKeys
        == "run,cleartext_input,:::anInstance::referenced,:::anInstance::cleartext_prop"
    )
    digest = job.manifest.manifest.config["changes"][0]["digestValue"]
    assert digest == "6070d076ffbf78675ba9adec0ad1823a99d63cd0"

    filepath = FilePath(__file__ + "/../fixtures/helmrepo")
    digestContents = filepath.__digestable__(dict(manifest=manifest))
    assert digestContents == "git:800472c7b1b2ea128464b9144c1440ca7289a5fa"

    with caplog.at_level(logging.DEBUG):
        manifest2 = YamlManifest(
            job.out.getvalue(), path=os.path.dirname(path), localEnv=manifest.localEnv
        )
        output2 = io.StringIO()  # so we don't save the file
        job2 = Runner(manifest2).run(JobOptions(startTime=2, out=output2))
        assert not job2.unexpectedAbort, job2.unexpectedAbort.get_stack_trace()
        digestKeys = job2.manifest.manifest.config["changes"][0]["digestKeys"]
        assert (
            digestKeys
            == "run,cleartext_input,:::anInstance::referenced,:::anInstance::cleartext_prop"
        )
        # print(job2.out.getvalue())
        summary = job2.json_summary()
        # print(json.dumps(summary, indent=2))
        assert summary == {
            "job": {
                "id": "A01120000000",
                "status": "ok",
                "total": 0,
                "ok": 0,
                "error": 0,
                "unknown": 0,
                "skipped": 0,
                "changed": 0,
            },
            "outputs": {},
            "tasks": [],
        }
        logMsg = "skipping task configure for instance nodeA with state NodeState.configured and status Status.ok (reassigned: None): no change detected"
        assert logMsg in caplog.text


class DependencyTest(unittest.TestCase):
    def test_bad_dependency(self):
        """
        Don't run a task if it depends on a live attribute on an non-operational instance.

        C is deployed after A and B and
          A depends on a C property (which are static)
          B depends on a C attribute (which are live)

        So A should run and B shouldn't run
        """
        self.maxDiff = None
        manifest = YamlManifest(manifestErrorContent)
        assert "attr" in list(
            manifest.tosca.topology.get_node_template("nodeC").attributeDefs
        )
        runner = Runner(manifest)
        job = runner.run(JobOptions(startTime=1))  # deploy
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        summary = job.json_summary()
        # print("deployed")
        # print(json.dumps(summary, indent=2))
        # print(job.out.getvalue())

        dependencies = [dict(ref=":::nodeC::prop", expected="static", required=True)]
        self.assertEqual(
            dependencies,
            job.manifest.manifest.config["changes"][0]["dependencies"],
        )

        self.assertEqual(
            summary,
            {
                "job": {
                    "id": "A01110000000",
                    "status": "error",
                    "total": 3,
                    "ok": 1,
                    "blocked": 1,
                    "error": 1,
                    "unknown": 0,
                    "skipped": 0,
                    "changed": 2,
                },
                "outputs": {},
                "tasks": [
                    {
                        "status": "ok",
                        "target": "nodeA",
                        "operation": "configure",
                        "template": "nodeA",
                        "type": "tosca.nodes.Root",
                        "targetStatus": "ok",
                        "targetState": "configured",
                        "changed": True,
                        "configurator": "unfurl.configurators.TemplateConfigurator",
                        "priority": "required",
                        "reason": "add",
                    },
                    {
                        "status": "ok",
                        "target": "nodeC",
                        "operation": "configure",
                        "template": "nodeC",
                        "type": "nodes.Test",
                        "targetStatus": "error",
                        "targetState": "configured",
                        "changed": True,
                        "configurator": "unfurl.configurators.TemplateConfigurator",
                        "priority": "required",
                        "reason": "add",
                    },
                    {
                        "status": "error",
                        "target": "nodeB",
                        "operation": "configure",
                        "template": "nodeB",
                        "type": "tosca.nodes.Root",
                        "targetStatus": "pending",
                        "targetState": "configuring",  # never ran
                        "changed": False,
                        "configurator": "unfurl.configurators.TemplateConfigurator",
                        "priority": "required",
                        "reason": "add",
                    },
                ],
            },
        )

    def test_dependencies(self):
        """
        Don't run a task if it depends on a live attribute on an non-operational instance.

        C is deployed after A and B and
          A depends on a C property (which are static)
          B depends on a C attribute (which are live)

        So B should run after C
        """
        self.maxDiff = None
        manifest = YamlManifest(manifestContent)
        runner = Runner(manifest)
        job = runner.run(JobOptions(startTime=1))  # deploy
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        summary = job.json_summary()
        # print("deployed")
        # print(json.dumps(summary, indent=2))
        # print(job.out.getvalue())

        # dependencies detected during render should be saved
        dependencies = [dict(ref=":::nodeC::attr", required=True, expected="Default")]
        self.assertEqual(
            dependencies,
            job.manifest.manifest.config["changes"][2]["dependencies"],
            job.manifest.manifest.config["changes"],
        )

        self.assertEqual(
            summary,
            {
                "job": {
                    "id": "A01110000000",
                    "status": "ok",
                    "total": 3,
                    "ok": 3,
                    "error": 0,
                    "unknown": 0,
                    "skipped": 0,
                    "changed": 3,
                },
                "outputs": {},
                "tasks": [
                    {
                        "status": "ok",
                        "target": "nodeA",
                        "operation": "configure",
                        "template": "nodeA",
                        "type": "tosca.nodes.Root",
                        "targetStatus": "ok",
                        "targetState": "configured",
                        "changed": True,
                        "configurator": "unfurl.configurators.TemplateConfigurator",
                        "priority": "required",
                        "reason": "add",
                    },
                    {
                        "status": "ok",
                        "target": "nodeC",
                        "operation": "configure",
                        "template": "nodeC",
                        "type": "nodes.Test",
                        "targetStatus": "ok",
                        "targetState": "configured",
                        "changed": True,
                        "configurator": "unfurl.configurators.TemplateConfigurator",
                        "priority": "required",
                        "reason": "add",
                    },
                    {
                        "status": "ok",
                        "target": "nodeB",
                        "operation": "configure",
                        "template": "nodeB",
                        "type": "tosca.nodes.Root",
                        "targetStatus": "ok",
                        "targetState": "configured",
                        "changed": True,
                        "configurator": "unfurl.configurators.TemplateConfigurator",
                        "priority": "required",
                        "reason": "add",
                    },
                ],
            },
        )

        # Deploy again: B's task should run now since C should have been deployed
        manifest2 = YamlManifest(job.out.getvalue())
        job = Runner(manifest2).run(JobOptions(startTime=2))
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        summary = job.json_summary()
        # changes = job.manifest.manifest.config["changes"]
        # XXX test that attr: "live" is in changes
        # print(job.out.getvalue())
        self.assertEqual(
            summary,
            {
                "job": {
                    "id": "A01120000000",
                    "status": "ok",
                    "total": 0,
                    "ok": 0,
                    "error": 0,
                    "unknown": 0,
                    "skipped": 0,
                    "changed": 0,
                },
                "outputs": {},
                "tasks": [],
            },
        )


def test_static_dependencies():
    """"""
    manifest = YamlManifest(static_dep_manifest)
    runner = Runner(manifest)
    job = runner.run(JobOptions(startTime=1))  # deploy
    assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
    summary = job.json_summary(add_rendered=True)
    summary["tasks"][0]["rendered_paths"] = []
    summary["tasks"][1]["rendered_paths"] = []
    assert summary == {
        "job": {
            "id": "A01110000000",
            "status": "error",
            "total": 2,
            "ok": 0,
            "blocked": 1,
            "error": 1,
            "unknown": 0,
            "skipped": 0,
            "changed": 1,
        },
        "outputs": {},
        "tasks": [
            {
                "status": "error",
                "target": "my_host",
                "operation": "configure",
                "template": "my_host",
                "type": "my_host_type",
                "targetStatus": "unknown",
                "targetState": "configuring",
                "changed": True,
                "configurator": "unfurl.configurators.shell.ShellConfigurator",
                "priority": "required",
                "reason": "add",
                "rendered_paths": [],
                "output": {
                    "args": "exit 1",
                    "cmd": "exit 1",
                    "error": None,
                    "returncode": 1,
                    "stderr": "",
                    "stdout": "",
                    "timeout": None,
                },
            },
            {
                "status": "error",
                "target": "my_node",
                "operation": "create",
                "template": "my_node",
                "type": "tosca.nodes.SoftwareComponent",
                "targetStatus": "pending",
                "targetState": "creating",
                "changed": False,
                "configurator": "unfurl.configurators.shell.ShellConfigurator",
                "priority": "required",
                "reason": "add",
                "rendered_paths": [],
                "output": "could not run: required dependencies not ready: my_host is unknown",
            },
        ],
    }


unconditional_manifest = """
apiVersion: unfurl/v1.0.0
kind: Ensemble
spec:
  service_template:
    node_types:
      my_host_type:
        derived_from: tosca:Root
        requirements:
          - test:
              node: tosca:Root
        interfaces:
          Standard:
            operations:
              configure:
                implementation: exit 1

    topology_template:
      node_templates:
        missing:
          type: my_host_type
"""
conditional_manifest = (
    unconditional_manifest
    + """
          directives:
          - conditional
"""
)

found_template = """\
          requirements:
          - test:
              node: depends_on_missing
"""

depends_on_missing_template = """\
        depends_on_missing:
          type: my_host_type
          requirements:
          - test:
              node: missing
              """

depends_on_missing_manifest = conditional_manifest + depends_on_missing_template

depends_on_found_manifest = (
    conditional_manifest + found_template + depends_on_missing_template
)


@pytest.mark.parametrize(
    "spec, success",
    [
        ("unconditional_manifest", False),
        ("conditional_manifest", True),
        ("depends_on_missing_manifest", False),
        ("depends_on_found_manifest", True),
    ],
)
def test_conditional_directive(spec, success):
    manifest = YamlManifest(globals()[spec])
    runner = Runner(manifest)
    try:
        job = runner.run(JobOptions(startTime=1, planOnly=True))
    except UnfurlValidationError:
        # traceback.print_exc()
        assert not success, "unexpected validation error"
    else:
        assert success, "expected a validation error"
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        summary = job.json_summary()
        assert summary["job"] == {
            "id": "A01110000000",
            "status": "ok",
            "total": 0,
            "ok": 0,
            "error": 0,
            "unknown": 0,
            "skipped": 0,
            "changed": 0,
        }


def _node_names(*reqlists):
    return [[r.target.name for r in reqs] for reqs in reqlists]


def test_reorder(mocker):
    # test that tasks dependent on a task whose rendering is blocked run after the blocked task is unblocked and not before.
    spy = mocker.spy(Job, "_reorder_requests")
    manifest = YamlManifest(reorderManifest)
    runner = Runner(manifest)
    job = runner.run(JobOptions(startTime=1))  # deploy
    assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
    summary = job.json_summary()
    assert spy.call_count == 3
    assert _node_names(*spy.call_args_list[0].args) == [
        ["nodeA", "nodeC", "nodeD"],
        ["nodeB"],
    ]
    # B is blocked on C, D requires B, so _reorder_requests moves D after B
    assert _node_names(*spy.spy_return_list[0]) == [
        ["nodeA", "nodeC"],
        ["nodeB", "nodeD"],
    ]
    # print(job.json_summary(True))
    assert summary["job"] == {
        "id": "A01110000000",
        "status": "ok",
        "total": 4,
        "ok": 4,
        "error": 0,
        "unknown": 0,
        "skipped": 0,
        "changed": 4,
    }
    assert [t["target"] for t in summary["tasks"]] == [
        "nodeA",
        "nodeC",
        "nodeB",
        "nodeD",
    ]


transientTemplate = """
apiVersion: unfurl/v1.0.0
kind: Ensemble
changes: [] # add so changes are saved here
spec:
  service_template:
    types:
      # declare the attribute as a property in a base type to test that
      # a property can be "promoted" to an attribute in a derived class
      nodes.Test:
        derived_from: tosca.nodes.Root
        properties:
          regular:
            type: string
            default: 
              eval:
                inert: regular
                substitute: <<FOO>>
          transient:
            type: string
            default: "t{{ now() }}" 
            metadata:
              inert: true

    topology_template:
      node_templates:
        nodeB:
          type: tosca:Root
          interfaces:
           Standard:
            operations:
              configure:
                implementation: Template
                inputs:
                  run: 
                    A:
                      eval:
                        get_ensemble_metadata: job
                    B: 
                      eval: :::nodeC::prop
                    C:
                      eval: $inputs::transient
                  done:
                    status: ok
                  transient:
                    type: string
                    default: "{{ range(1,100) | random }}"
                    metadata:
                      inert: <<RANDOM>>

        nodeC:
          type: nodes.Test
          properties:
            prop: >
              embedded {{ SELF.regular }} {{ SELF.transient | upper | inert }}
"""


def _run(template, expected_digest, ok, reason):
    manifest = YamlManifest(template)
    runner = Runner(manifest)
    output = io.StringIO()  # so we don't save the file
    job = runner.run(JobOptions(out=output))
    assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
    summary = job.json_summary()
    plans = job._json_plan_summary()
    assert summary["job"]["status"] == "ok"
    assert summary["job"]["ok"] == ok
    plan = plans[0]["plan"][0]
    if "sequence" in plan:
        plan = plan["sequence"][0]
    assert plan["reason"] == reason, plan
    # pprint.pprint(job.manifest.manifest.config["changes"])
    # assert digest list
    digestKeys = job.manifest.manifest.config["changes"][-1]["digestKeys"]
    expectedKeys = "run,transient,:::nodeC::prop,:::nodeC::regular,:::nodeC::transient"
    assert digestKeys == expectedKeys
    digest = job.manifest.manifest.config["changes"][-1]["digestValue"]
    assert digest == expected_digest
    # print(job.out.getvalue())
    return job


def test_transient():
    digest = "94cbbf1056963f084efc0f584843095774cb50f9"
    job = _run(transientTemplate, digest, 1, "add")
    assert (
        serialize_value(
            job.rootResource.query(":::nodeC::prop", wantList="result")[0], redact=True
        )
        == "embedded <<FOO>> <<REPLACED>>"
    )
    # no changes when run again:
    job2 = _run(job.out.getvalue(), digest, 0, "reconfigure")

    # modified  # change dict and template, assert task reruns
    template = job2.out.getvalue().replace("inert: regular", "inert: modified")
    digest = "3c505df64b107ebd7f8765e5161ab22ecdf405bf"
    job3 = _run(template, digest, 1, "reconfigure")

    # no changes when run again:
    _run(job3.out.getvalue(), digest, 0, "reconfigure")


# --- Resume task tests ---

from unfurl.configurator import Configurator, Cancel, Status


class ResumeTestConfigurator(Configurator):
    """Test configurator that yields resume twice, then completes."""

    def run(self, task):
        task.logger.info("resume configurator: first run")
        signal = yield task.suspend()
        if isinstance(signal, Cancel):
            yield task.done(False, result={"cancelled": signal.reason})
            return
        task.logger.info("resume configurator: second run")
        signal = yield task.suspend()
        if isinstance(signal, Cancel):
            yield task.done(False, result={"cancelled": signal.reason})
            return
        task.logger.info("resume configurator: completing")
        yield task.done(True, status=Status.ok)


resume_manifest = """
apiVersion: unfurl/v1.0.0
kind: Ensemble
spec:
  service_template:
    node_types:
      test.ResumeHost:
        derived_from: tosca:Root
        capabilities:
          host:
            type: tosca.capabilities.Compute

    topology_template:
      node_templates:
        resume_node:
          type: test.ResumeHost
          interfaces:
            Standard:
              operations:
                configure:
                  implementation:
                    className: tests.test_dependencies.ResumeTestConfigurator

        dependent_node:
          type: tosca.nodes.SoftwareComponent
          requirements:
          - host: resume_node
          interfaces:
            Standard:
              operations:
                create:
                  implementation: Template
                  inputs:
                    done:
                      status: ok
"""


def test_resume_task():
    """Resume task completes after multiple resume cycles, dependent runs after."""
    manifest = YamlManifest(resume_manifest)
    runner = Runner(manifest)
    job = runner.run(JobOptions(startTime=1))
    assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
    summary = job.json_summary()
    # import json; print(json.dumps(summary, indent=2))
    assert summary["job"]["status"] == "ok"
    # Both tasks should have run successfully
    assert summary["job"]["ok"] == 2, summary
    task_targets = [t["target"] for t in summary["tasks"]]
    assert "resume_node" in task_targets
    assert "dependent_node" in task_targets


def test_resume_task_dependencies():
    """Dependent task waits for resume task to fully complete."""
    manifest = YamlManifest(resume_manifest)
    runner = Runner(manifest)
    job = runner.run(JobOptions(startTime=1))
    assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
    summary = job.json_summary()
    assert summary["job"]["status"] == "ok"
    task_targets = [t["target"] for t in summary["tasks"]]
    # dependent_node should appear after resume_node
    if "dependent_node" in task_targets and "resume_node" in task_targets:
        assert task_targets.index("resume_node") < task_targets.index(
            "dependent_node"
        )


def test_resume_task_cancel():
    """Job timeout triggers Cancel on resume task."""
    manifest = YamlManifest(resume_manifest)
    runner = Runner(manifest)
    # Very short timeout to trigger cancellation during resume
    job = runner.run(JobOptions(startTime=1, timeout=0.001))
    # Job should end in error due to timeout
    assert job.status == Status.error


deferred_release_manifest = """
apiVersion: unfurl/v1.0.0
kind: Ensemble
spec:
  service_template:
    node_types:
      test.ResumeHost:
        derived_from: tosca:Root
        capabilities:
          host:
            type: tosca.capabilities.Compute

      test.IndependentResume:
        derived_from: tosca:Root

    topology_template:
      node_templates:
        resume_host:
          type: test.ResumeHost
          interfaces:
            Standard:
              operations:
                configure:
                  implementation:
                    className: tests.test_dependencies.ResumeTestConfigurator

        independent_resume:
          type: test.IndependentResume
          interfaces:
            Standard:
              operations:
                configure:
                  implementation:
                    className: tests.test_dependencies.ResumeTestConfigurator

        dependent_node:
          type: tosca.nodes.SoftwareComponent
          requirements:
          - host: resume_host
          interfaces:
            Standard:
              operations:
                create:
                  implementation: Template
                  inputs:
                    done:
                      status: ok
"""


def test_resume_deferred_release():
    """Deferred tasks only release when their specific blocker completes, not any resume."""
    manifest = YamlManifest(deferred_release_manifest)
    runner = Runner(manifest)
    job = runner.run(JobOptions(startTime=1))
    assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
    summary = job.json_summary()
    assert summary["job"]["status"] == "ok", summary
    assert summary["job"]["error"] == 0, summary
    assert summary["job"]["ok"] == 3, summary
    task_targets = [t["target"] for t in summary["tasks"]]
    assert "resume_host" in task_targets
    assert "independent_resume" in task_targets
    assert "dependent_node" in task_targets
    # dependent_node must run after resume_host (its dependency), not after independent_resume
    assert task_targets.index("resume_host") < task_targets.index("dependent_node")


class SequentialSubtaskConfigurator(Configurator):
    """Yields two subtask PlanRequests sequentially, each using background shell."""

    def run(self, task):
        sub1 = task.create_sub_task("Custom.op1", task.target)
        assert sub1
        result1 = yield sub1
        assert result1 is not None, "subtask 1 result was None"
        task.logger.info("subtask 1 done")

        sub2 = task.create_sub_task("Custom.op2", task.target)
        assert sub2
        result2 = yield sub2
        assert result2 is not None, "subtask 2 result was None"
        task.logger.info("subtask 2 done")

        yield task.done(True)


sequential_subtask_manifest = """
apiVersion: unfurl/v1.0.0
kind: Ensemble
spec:
  service_template:
    node_types:
      Test:
        derived_from: tosca.nodes.Root
        interfaces:
          Custom:
            type: tosca.interfaces.Root
            operations:
              op1:
                implementation: echo start_op1
                inputs:
                  background: true
                  done:
                    success: true
              op2:
                implementation: echo start_op2
                inputs:
                  background: true

    topology_template:
      node_templates:
        parent_node:
          type: Test
          interfaces:
            Standard:
              operations:
                configure:
                  implementation:
                    className: tests.test_dependencies.SequentialSubtaskConfigurator
                start:
                  implementation: echo start_output
                  inputs:
                    background: true
"""


def test_resume_sequential_subtasks():
    """Parent yields two background shell subtasks sequentially; both complete."""
    manifest = YamlManifest(sequential_subtask_manifest)
    runner = Runner(manifest)
    job = runner.run(JobOptions(startTime=1))
    assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
    summary = job.json_summary()
    assert summary == {
        "job": {
            "id": "A01110000000",
            "status": "ok",
            "total": 4,
            "ok": 4,
            "error": 0,
            "unknown": 0,
            "skipped": 0,
            "changed": 1,
        },
        "outputs": {},
        "tasks": [
            {
                "status": "ok",
                "target": "parent_node",
                "operation": "configure",
                "template": "parent_node",
                "type": "Test",
                "targetStatus": "pending",
                "targetState": "configured",
                "changed": False,
                "configurator": "tests.test_dependencies.SequentialSubtaskConfigurator",
                "priority": "required",
                "reason": "add",
            },
            {
                "status": "ok",
                "target": "parent_node",
                "operation": "op1",
                "template": "parent_node",
                "type": "Test",
                "targetStatus": "pending",
                "targetState": "configuring",
                "changed": False,
                "configurator": "unfurl.configurators.shell.ShellConfigurator",
                "priority": "required",
                "reason": "subtask: for add: Standard.configure",
            },
            {
                'changed': True,
                'configurator': 'unfurl.configurators.shell.ShellConfigurator',
                "operation": "start",
                'priority': 'required',
                'reason': "add",
                'status': "ok",
                'target': 'parent_node',
                'targetState': 'started',
                'targetStatus': 'ok',
                'template': 'parent_node',
                'type': 'Test',
            },
            {
                "status": "ok",
                "target": "parent_node",
                "operation": "op2",
                "template": "parent_node",
                "type": "Test",
                "targetStatus": "pending",
                "targetState": "configuring",
                "changed": False,
                "configurator": "unfurl.configurators.shell.ShellConfigurator",
                "priority": "required",
                "reason": "subtask: for add: Standard.configure",
            },
        ],
    }


class NestedSubtaskConfigurator(Configurator):
    """Yields a subtask that uses a background shell command."""

    def run(self, task):
        sub = task.create_sub_task("Install.check", task.target)
        assert sub
        result = yield sub
        assert result is not None, "subtask result was None"
        task.logger.info("nested subtask done")
        yield task.done(True, status=Status.ok)


nested_subtask_manifest = """
apiVersion: unfurl/v1.0.0
kind: Ensemble
spec:
  service_template:
    topology_template:
      node_templates:
        parent_node:
          type: tosca:Root
          interfaces:
            Install:
              check:
                implementation: echo nested_check_output
                inputs:
                  background: true
                  done:
                    success: true
            Standard:
              operations:
                configure:
                  implementation:
                    className: tests.test_dependencies.NestedSubtaskConfigurator
"""


def test_resume_nested_subtasks():
    """A parent configurator yields a subtask that uses background shell with resume."""
    manifest = YamlManifest(nested_subtask_manifest)
    runner = Runner(manifest)
    job = runner.run(JobOptions(startTime=1))
    assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
    summary = job.json_summary()
    assert summary == {
        "job": {
            "id": "A01110000000",
            "status": "ok",
            "total": 2,
            "ok": 2,
            "error": 0,
            "unknown": 0,
            "skipped": 0,
            "changed": 1,
        },
        "outputs": {},
        "tasks": [
            {
                "status": "ok",
                "target": "parent_node",
                "operation": "configure",
                "template": "parent_node",
                "type": "tosca.nodes.Root",
                "targetStatus": "ok",
                "targetState": "configured",
                "changed": True,
                "configurator": "tests.test_dependencies.NestedSubtaskConfigurator",
                "priority": "required",
                "reason": "add",
            },
            {
                "status": "ok",
                "target": "parent_node",
                "operation": "check",
                "template": "parent_node",
                "type": "tosca.nodes.Root",
                "targetStatus": "pending",
                "targetState": "configuring",
                "changed": False,
                "configurator": "unfurl.configurators.shell.ShellConfigurator",
                "priority": "required",
                "reason": "subtask: for add: Standard.configure",
            },
        ],
    }


class PauseTestConfigurator(Configurator):
    """Yields suspend(pause=0.2) then completes."""

    def run(self, task):
        task.logger.info("pause configurator: suspending with pause")
        signal = yield task.suspend(pause=0.2)
        if isinstance(signal, Cancel):
            yield task.done(False, result={"cancelled": signal.reason})
            return
        task.logger.info("pause configurator: resumed after pause")
        yield task.done(True, status=Status.ok)


pause_manifest = """
apiVersion: unfurl/v1.0.0
kind: Ensemble
spec:
  service_template:
    topology_template:
      node_templates:
        pause_node:
          type: tosca:Root
          interfaces:
            Standard:
              operations:
                configure:
                  implementation:
                    className: tests.test_dependencies.PauseTestConfigurator
"""


def test_resume_with_pause():
    """suspend(pause=N) delays re-entry by at least N seconds."""
    manifest = YamlManifest(pause_manifest)
    runner = Runner(manifest)
    start = time.monotonic()
    job = runner.run(JobOptions(startTime=1))
    elapsed = time.monotonic() - start
    assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
    summary = job.json_summary()
    assert summary["job"]["status"] == "ok", summary
    assert summary["job"]["ok"] == 1, summary
    assert elapsed >= 0.2, f"Expected >= 0.2s, got {elapsed:.3f}s"


class PauseSubtaskConfigurator(Configurator):
    """Yields a subtask that uses suspend(pause=...)."""

    def run(self, task):
        sub = task.create_sub_task("Install.check", task.target)
        assert sub
        result = yield sub
        assert result is not None, "subtask result was None"
        task.logger.info("pause subtask done")
        yield task.done(True, status=Status.ok)


pause_subtask_manifest = """
apiVersion: unfurl/v1.0.0
kind: Ensemble
spec:
  service_template:
    topology_template:
      node_templates:
        parent_node:
          type: tosca:Root
          interfaces:
            Install:
              check:
                implementation:
                  className: tests.test_dependencies.PauseTestConfigurator
            Standard:
              operations:
                configure:
                  implementation:
                    className: tests.test_dependencies.PauseSubtaskConfigurator
"""


def test_resume_subtask_with_pause():
    """A subtask using suspend(pause=N) propagates the pause to the parent."""
    manifest = YamlManifest(pause_subtask_manifest)
    runner = Runner(manifest)
    start = time.monotonic()
    job = runner.run(JobOptions(startTime=1))
    elapsed = time.monotonic() - start
    assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
    summary = job.json_summary()
    assert summary["job"]["status"] == "ok", summary
    assert elapsed >= 0.2, f"Expected >= 0.2s, got {elapsed:.3f}s"
