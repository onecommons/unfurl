"""An illustrative TOSCA service template"""


import unfurl
from typing import Sequence, Union
import tosca
from tosca import Attribute, Eval, GB, MB, Property
import unfurl.configurators.shell


class dbconnection(tosca.relationships.ConnectsTo):
    username: str
    password: str = Property(metadata={"sensitive": True})


class myApplication(tosca.nodes.SoftwareComponent):
    domain: str = Eval({"get_input": "domain"})

    private_address: str = Attribute()

    host: Sequence[
        Union[tosca.relationships.HostedOn, tosca.nodes.Compute, tosca.capabilities.Compute]
    ] = ()
    db: "dbconnection"

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


compute = tosca.nodes.Compute(
    "compute",
    host=tosca.capabilities.Compute(
        num_cpus=1,
        disk_size=200 * GB,
        mem_size=512 * MB,
    ),
)

mydb = tosca.nodes.Database(
    "mydb",
    name="mydb",
    host=compute
)

mydb_connection = dbconnection(
    "mydb_connection",
    username="myapp",
    password=Eval({"eval": {"secret": "myapp_db_pw"}}),
)
myApp = myApplication(
    "myApp",
    host=[compute],
    db=mydb_connection[mydb],
)
myApp.image = tosca.artifacts.DeploymentImageContainerDocker(
    "image",
    file="myapp:latest",
    repository="docker_hub",
)


__all__ = [
    "dbconnection",
    "myApplication",
    "myApp",
    "compute",
    "mydb",
    "mydb_connection",
]

