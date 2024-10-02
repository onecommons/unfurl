from unfurl.configurator import Configurator


def expressionFunc(ctx, arg):
    # print(f'LocallyDefinedConfigurator at {__file__}')
    return arg


class LocallyDefinedConfigurator(Configurator):
    def run(self, task):
        task.logger.info(f"LocallyDefinedConfigurator at {__file__}")
        task.target.attributes["anAttribute"] = "set"
        yield task.done(True)
