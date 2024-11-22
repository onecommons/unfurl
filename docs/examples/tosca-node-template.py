import tosca
from tosca import GB, MB

compute = tosca.nodes.Compute(
    host=tosca.capabilities.Compute(
        num_cpus=1,
        disk_size=200 * GB,
        mem_size=512 * MB,
    ),
)

mydb = PostgresDB(name="mydb")

myApp = MyApplication(
    ports=tosca.datatypes.NetworkPortSpec("80:8080"),
    host=[compute],
    db=mydb,
)
myApp.image = tosca.artifacts.DeploymentImageContainerDocker(
    "image",
    file="myapp:latest",
    repository="docker_hub",
)
