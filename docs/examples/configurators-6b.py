from unfurl.configurators.terraform import (
    TerraformConfigurator,
    TerraformInputs,
)
from unfurl.tosca_plugins.expr import tfvar, tfoutput
import tosca

class ExampleTerraformManagedResource(tosca.nodes.Root):
    """A type of resource that is managed (create/update/delete) by a terraform resource"""

    # TOSCA properties are conceptually similar to Terraform variables.
    # Set the tfvar option to indicate the property should set a Terraform variable with the same name.
    example_terraform_var: str = tosca.Property(options=tfvar)
    """A property the UI will render for user input"""

    # TOSCA attributes are conceptually similar to Terraform outputs.
    # Set the tfoutput option to indicate the attribute should be set to the terraform output with the same name.
    example_terraform_output: str = tosca.Attribute(options=tfoutput)

    # call this function for the basic CRUD operations in the TOSCA deploy workflow
    @tosca.operation(apply_to=["Install.check", "Standard.configure", "Standard.delete"])
    def default(self, **kw) -> TerraformConfigurator:
        # Implement these operations using the Terraform module found in the directory named "terraform"
        return TerraformConfigurator(TerraformInputs(main="terraform"))
