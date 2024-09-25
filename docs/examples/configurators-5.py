import unfurl
import tosca
from unfurl.tosca_plugins.artifacts import artifact_AsdfTool

@tosca.operation(name="configure")
def terraform_example_configure(**kw):
    return unfurl.configurators.shell.ShellConfigurator(
        command=["ripgrep"],
        cmd="rg search",
    )

terraform_example = tosca.nodes.Root(
    "terraform-example",
)
terraform_example.ripgrep = artifact_AsdfTool(
    "ripgrep",
    version="13.0.0",
    file="ripgrep",
)
terraform_example.configure = terraform_example_configure
