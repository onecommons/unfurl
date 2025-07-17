import unfurl.configurators.ansible
import unfurl.configurators.shell
import tosca
from tosca import operation


class Example(tosca.nodes.Root):
    _interface_requirements = ["unfurl.relationships.ConnectsTo.GoogleCloudProject"]

    def create(self, **kw):
        return unfurl.configurators.shell.ShellConfigurator(
            command="./myscript.sh",
            foo="bar",
        )

    @operation(
        environment={"AN_ENV_VAR": "1"},
        outputs={"an_ansible_fact": "an_attribute"},
        dependencies=["a_ansible_collection"],
    )
    def configure(self, **kw):
        return unfurl.configurators.ansible.AnsibleConfigurator(
            foo="baz",
        )
