import unfurl
from typing import Any, Sequence
import tosca
from tosca import Attribute
import unfurl.configurators.shell
from unfurl.tosca_plugins.expr import get_input

class MyApplication(tosca.nodes.SoftwareComponent):
    domain: str = get_input("domain")
    ports: "tosca.datatypes.NetworkPortSpec"

    private_address: str = Attribute()

    host: Sequence[
        "tosca.relationships.HostedOn | tosca.nodes.Compute | tosca.capabilities.Compute"
    ] = ()
    db: "tosca.relationships.ConnectsTo | capabilities_postgresdb"

    def create(self, **kw):
        return unfurl.configurators.shell.ShellConfigurator(
            command=["create.sh"],
        )

    def configure(self, **kw):
        return unfurl.configurators.shell.ShellConfigurator(
            command=["configure.sh"],
        )

    def delete(self, **kw):
        return unfurl.configurators.shell.ShellConfigurator(
            command=["delete.sh"],
        )
