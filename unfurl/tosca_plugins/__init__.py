TOSCA_VERSION = "tosca_simple_unfurl_1_0_0"

# loaded by toscaparser extension manager using entrypoint declared in setup.cfg
class plugindef_1_0_0:
    VERSION = TOSCA_VERSION

    DEFS_FILE = "tosca-ext.yaml"

    SECTIONS = ("metadata", "decorators", "deployment_blueprints", "input_values")
