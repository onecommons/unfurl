from ..configurator import Configurator, Status


class TemplateConfigurator(Configurator):
    def processResultTemplate(self, task, result):
        # get the resultTemplate without evaluating it
        resultTemplate = task.inputs._attributes.get("resultTemplate")
        if resultTemplate:  # evaluate it now with the result
            results = task.query({"eval": dict(template=resultTemplate)}, vars=result)
            if results and results.strip():
                task.updateResources(results)

    def run(self, task):
        # XXX handle outputs better
        self.processResultTemplate(task, self.configSpec.outputs)
        yield task.done()


class Delegate(Configurator):
    def canDryRun(self, task):
        return True # ok because this will also be called on the subtask

    def run(self, task):
        subtaskRequest = task.createSubTask(task.inputs["operation"], task.inputs.get('target'))
        assert subtaskRequest
        # note: this will call canRun() and if needed canDryRun() on subtask but not shouldRun()
        subtask = yield subtaskRequest
        yield subtask.result
