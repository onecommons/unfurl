import six
from ..configurator import Configurator, Status


class TemplateConfigurator(Configurator):
    def processResultTemplate(self, task, result):
        # get the resultTemplate without evaluating it
        resultTemplate = task.inputs._attributes.get("resultTemplate")
        if resultTemplate:  # evaluate it now with the result
            if isinstance(resultTemplate, six.string_types):
                query = dict(template=resultTemplate)
            else:
                query = resultTemplate
            results = task.query({"eval": query}, vars=result)
            if results:
                if isinstance(results, six.string_types):
                    result = results.strip()
                task.updateResources(results)

    def run(self, task):
        result = task.inputs.get("result", {})
        self.processResultTemplate(task, result.get("result"))
        yield task.done(**result)


class Delegate(Configurator):
    def canDryRun(self, task):
        return True  # ok because this will also be called on the subtask

    def run(self, task):
        subtaskRequest = task.createSubTask(
            task.inputs["operation"], task.inputs.get("target")
        )
        assert subtaskRequest
        # note: this will call canRun() and if needed canDryRun() on subtask but not shouldRun()
        subtask = yield subtaskRequest
        yield subtask.result
