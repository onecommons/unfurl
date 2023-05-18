# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
import os
from ..eval import Ref, map_value
from ..configurator import Configurator, TaskView
from ..result import Results, ResultsMap
from ..util import register_short_names
from ..support import Status
from ..planrequests import set_default_command
from ..runtime import EntityInstance
import importlib
from typing import Sequence, Tuple
from collections.abc import Mapping

# need to define these now because these configurators are lazily imported
# and so won't register themselves through AutoRegisterClass
register_short_names(
    {
        name: f"unfurl.configurators.{name.lower()}.{name}Configurator"
        for name in "Ansible Shell Supervisor Terraform DNS Kompose".split()
    }
)


class CmdConfigurator(Configurator):
    @classmethod
    def set_config_spec_args(klass, kw: dict, target: EntityInstance):
        return set_default_command(kw, "")


class PythonPackageCheckConfigurator(Configurator):
    def run(self, task):
        try:
            importlib.import_module(task.target.file)
            status = Status.ok
        except ImportError:
            status = Status.absent
        except Exception:
            status = Status.error
        yield task.done(True, status=status)


class TemplateConfigurator(Configurator):
    exclude_from_digest: Tuple[str, ...] = ("resultTemplate", "done")

    def process_result_template(self, task: "TaskView", result):
        """
        for both the ansible and shell configurators
        result can include: "returncode", "msg", "error", "stdout", "stderr"
        Ansible also includes "outputs"
        """
        # get the resultTemplate without evaluating it
        resultTemplate = task.inputs._attributes.get("resultTemplate")
        errors: Sequence[Exception] = []
        current_status = task.target.local_status
        if resultTemplate:  # evaluate it now with the result
            if os.getenv("UNFURL_TEST_DEBUG_EX"):
                task.logger.error(
                    "evaluated result template (%s) with %s %s",
                    type(resultTemplate),
                    type(result),
                    result,
                )
            if isinstance(resultTemplate, Results):
                resultTemplate = resultTemplate._attributes
            try:
                if os.getenv("UNFURL_TEST_DEBUG_EX"):
                    task.logger.error(
                        "result %s template is %s", type(resultTemplate), resultTemplate
                    )
                    trace = 2
                else:
                    trace = 0
                if Ref.is_ref(resultTemplate):
                    results = task.query(
                        {"eval": resultTemplate}, vars=result, throw=True
                    )
                else:
                    # lazily evaluated by update_instances() below
                    results = Results._map_value(
                        resultTemplate,
                        task.inputs.context.copy(vars=result, trace=trace),
                    )
            except Exception as e:
                results = None
                errors = [e]
                task.logger.debug(
                    "exception when processing resultTemplate", exc_info=True
                )
            else:
                if results:
                    jr, errors = task.update_instances(results)  # type: ignore
            if errors:
                task.logger.warning(
                    "error processing resultTemplate: %s",
                    errors[0],
                )
        if task.target.local_status != current_status:
            new_status = task.target.local_status
        else:
            new_status = None
        return errors, new_status

    def can_dry_run(self, task):
        return not not task.inputs.get("dryrun")

    def render(self, task):
        if task.dry_run:
            runResult = task.inputs.get("dryrun")
            if not isinstance(runResult, dict):
                runResult = task.inputs.get("run")
        else:
            runResult = task.inputs.get("run")
        return runResult

    def done(self, task: "TaskView", **kw):
        # this is called by derived classes like ShellConfigurator to allow the user
        # to override the default logic for updating the state and status of a task.
        done = task.inputs.get_copy("done", {})
        if done:
            task.logger.trace("evaluated done template with %s", done)
            kw.update(done)  # "done" overrides kw
        return task.done(**kw)

    def run(self, task: "TaskView"):
        runResult = task.rendered
        done = task.inputs.get_copy("done", {})
        if "result" not in done:
            if not isinstance(runResult, Mapping):
                done["result"] = {"run": runResult, "outputs": done.get("outputs")}
            else:
                done["result"] = runResult
        errors, new_status = self.process_result_template(
            task, done.get("result") or {}
        )
        if errors:
            done["success"] = False  # returned errors
        if new_status is not None:
            done["status"] = new_status
        yield task.done(**done)


class DelegateConfigurator(Configurator):
    def can_dry_run(self, task):
        # ok because this will also be called on the subtask
        return True

    def render(self, task):
        # should_run rendered already
        return task.rendered

    def should_run(self, task):
        task.rendered = task.create_sub_task(
            task.inputs.get("operation"),
            task.inputs.get("target"),
            task.inputs.get("inputs"),
        )
        # only run if create_sub_task() returned a TaskRequest
        if task.rendered:
            return task.rendered.configSpec.should_run()
        else:
            return False

    def run(self, task):
        if "when" in task.inputs and not task.inputs["when"]:
            # check this here instead of should_run() so that we always create and run the task
            # (really just a hack so we save the digest in the job log for future reconfigure operations)
            task.logger.debug(
                "skipping subtask: 'when' input evaluated to false: %s",
                task.configSpec.inputs["when"],
            )
            yield task.done(True, modified=False)
        else:
            subtaskRequest = task.rendered
            assert subtaskRequest
            # note: this will call can_run_task() for the subtask but not shouldRun()
            subtask = yield subtaskRequest
            yield subtask.result
