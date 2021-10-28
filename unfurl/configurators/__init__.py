# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
from ..configurator import Configurator
from ..result import ResultsMap
from ..util import register_short_names
from ..support import Status
import importlib

# need to define these now because these configurators are lazily imported
# and so won't register themselves through AutoRegisterClass
register_short_names(
    {
        name: f"unfurl.configurators.{name.lower()}.{name}Configurator"
        for name in "Ansible Shell Supervisor Terraform DNS".split()
    }
)


class PythonPackageCheckConfigurator(Configurator):
    def run(self, task):
        try:
            importlib.import_module(task.target.file)
            status = Status.ok
        except ImportError:
            status = Status.absent
        except:
            status = Status.error
        yield task.done(True, status=status)


class TemplateConfigurator(Configurator):
    exclude_from_digest = ("resultTemplate", "done")

    def process_result_template(self, task, result):
        """
        for both the ansible and shell configurators
        result can include: "returncode", "msg", "error", "stdout", "stderr"
        Ansible also includes "outputs"
        """
        # get the resultTemplate without evaluating it
        resultTemplate = task.inputs._attributes.get("resultTemplate")
        errors = []
        if resultTemplate:  # evaluate it now with the result
            if isinstance(resultTemplate, str):
                query = dict(template=resultTemplate)
            else:
                query = resultTemplate

            # workaround for jinja template processing setting Result when getting items
            if not isinstance(result, ResultsMap):
                vars = ResultsMap(result, task.inputs.context)
                vars.doFullResolve = True
            else:
                vars = result
            task.logger.trace("evaluated result template with %s", result)
            try:
                results = task.query({"eval": query}, vars=vars, throw=True)
            except Exception as e:
                results = None
                errors = [e]
            else:
                if results:
                    jr, errors = task.update_instances(results)
            if errors:
                task.logger.warning(
                    "error processing resultTemplate for %s: %s",
                    task.target.name,
                    errors[0],
                )
        return errors

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

    def done(self, task, **kw):
        done = task.inputs.get_copy("done", {})
        if done:
            task.logger.trace("evaluated done template with %s", done)
            kw.update(done)  # "done" overrides kw
        return task.done(**kw)

    def run(self, task):
        runResult = task.rendered
        done = task.inputs.get_copy("done", {})
        if "result" not in done:
            if not isinstance(runResult, dict):
                done["result"] = {"run": runResult}
            else:
                done["result"] = runResult
        if self.process_result_template(task, done.get("result")):
            done["success"] = False  # returned errors
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
        subtaskRequest = task.rendered
        assert subtaskRequest
        # note: this will call canRun() and if needed canDryRun() on subtask but not shouldRun()
        subtask = yield subtaskRequest
        yield subtask.result
