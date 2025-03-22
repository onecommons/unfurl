# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
import os
from typing_extensions import TypedDict

from tosca import ToscaInputs
from ..eval import Ref, map_value
from ..configurator import Configurator, TaskView
from ..result import Results, ResultsMap
from ..util import UnfurlTaskError, register_short_names
from ..support import Status
from ..planrequests import (
    set_default_command,
    ConfigurationSpecKeywords,
    TaskRequest,
)
import importlib
from typing import Any, Dict, Generator, List, Optional, Sequence, Tuple, Union, cast
from collections.abc import Mapping

# need to define these now because these configurators are lazily imported
# and so won't register themselves through AutoRegisterClass
register_short_names({
    name: f"unfurl.configurators.{name.lower()}.{name}Configurator"
    for name in "Ansible Shell Supervisor Terraform DNS Kompose".split()
})


class CmdConfigurator(Configurator):
    @classmethod
    def set_config_spec_args(cls, kw: dict, target):
        return set_default_command(cast(ConfigurationSpecKeywords, kw), "")


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


class DoneDict(TypedDict, total=False):
    success: Optional[bool]
    modified: Optional[bool]
    status: Optional[Status]
    result: Union[dict, str, None]
    outputs: Optional[dict]


class TemplateInputs(ToscaInputs):
    run: Optional[Any] = None
    dryrun: Union[None, bool, str, dict] = None
    done: Optional[DoneDict] = None
    resultTemplate: Optional[Dict] = None


class TemplateConfigurator(Configurator):
    exclude_from_digest: Tuple[str, ...] = ("resultTemplate", "done")

    def process_result_template(self, task: "TaskView", result: Dict[str, Any]):
        """
        for both the ansible and shell configurators
        result can include: "returncode", "msg", "error", "stdout", "stderr"
        Ansible also includes "outputs"
        """
        # get the resultTemplate without evaluating it
        resultTemplate = task.inputs.get_original("resultTemplate")
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
                    results = task.query(resultTemplate, vars=result, throw=True)
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
        return bool(task.inputs.get("dryrun"))

    def render(self, task: "TaskView"):
        if task.dry_run:
            runResult = task.inputs.get("dryrun")
            if not isinstance(runResult, dict):
                runResult = task.inputs.get("run")
        else:
            runResult = task.inputs.get("run")
        if isinstance(runResult, Mapping):
            logResult: Any = list(runResult)
        else:
            logResult = runResult
        task.logger.trace("render run template with %s", logResult)
        return runResult

    def done(self, task: "TaskView", **kw):
        # this is called by derived classes like ShellConfigurator to allow the user
        # to override the default logic for updating the state and status of a task.
        done: dict = task.inputs.get_copy("done", {})
        if done:
            task.logger.trace("evaluated done template with %s", done)
            kw.update(done)  # "done" overrides kw
        return task.done(**kw)

    def run(self, task: "TaskView") -> Generator:
        runResult = task.rendered
        done: dict = task.inputs.get_copy("done", {})
        if not isinstance(done, dict):
            raise UnfurlTaskError(task, 'input "done" must be a dict')
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
        task.inputs.get("when")  # evaluate now to determine dependencies
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
            assert isinstance(subtaskRequest, TaskRequest), subtaskRequest
            # note: this will call can_run_task() for the subtask but not shouldRun()
            subtask = yield subtaskRequest
            if not subtask.result and not subtaskRequest.required:
                # subtask was skipped
                # if skipping because subtask didn't support dry run, set modified to simulate
                modified = task.dry_run and task.configSpec.operation != "check"
                yield task.done(True, modified=modified)
            else:
                yield subtask.result
