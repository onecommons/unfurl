from unfurl.configurator import Configurator


class LocallyDefinedConfigurator(Configurator):
    def run(self, task):
        task.logger.info(__file__)
        task.target.attributes["anAttribute"] = "set"
        yield task.done(True)
