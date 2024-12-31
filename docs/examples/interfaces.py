import tosca
import unfurl.configurators.shell
import unfurl.configurators.ansible


class Example(tosca.nodes.Root):
    _interface_requirements = ["unfurl.relationships.ConnectsTo.GoogleCloudProject"]

    def create(self, **kw):
        return unfurl.configurators.shell.ShellConfigurator(
            command="./myscript.sh",
            foo="bar",
        )

    @tosca.operation(
        environment={"AN_ENV_VAR": 1}, 
        outputs={"an_ansible_fact": "an_attribute"}
    )
    def configure(self, **kw):
        return unfurl.configurators.ansible.AnsibleConfigurator(
            foo="baz",
        )
