from unfurl.tosca_plugins.artifacts import artifact_AsdfTool
import tosca

class artifact_example(tosca.nodes.Root):
    ripgrep: artifact_AsdfTool = artifact_AsdfTool(
        "ripgrep",
        version="13.0.0",
        file="ripgrep",
    )

    def configure(self, **kw):
        return self.ripgrep.execute(
            cmd="rg search",
        )

