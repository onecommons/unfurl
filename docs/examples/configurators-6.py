import tosca
import unfurl
from unfurl.tosca_plugins.artifacts import unfurl_nodes_Installer_Terraform
import unfurl.configurators.terraform


@tosca.operation(
    name="default", apply_to=["Install.check", "Standard.configure", "Standard.delete"]
)
def terraform_example_default(self, **kw):
    return unfurl.configurators.terraform.TerraformConfigurator(
        tfvars={"tag": "test"},
        main="""
      variable "tag" {
        type  = string
      }
      output "name" {
        value = var.tag
      }""",
    )

terraform_example = unfurl_nodes_Installer_Terraform()
terraform_example.set_operation(terraform_example_default)
