import tosca
from tosca import Eval, Repository

docker_hub = Repository(
    name="docker_hub",
    url="https://registry.hub.docker.com/",
    credential=tosca.datatypes.Credential(
        user="user1",
        token=Eval({"eval": {"secret": "dockerhub_user1_pw"}}),
    ),
)
myApp = tosca.nodes.Root()
myApp.image = tosca.artifacts.DeploymentImageContainerDocker(
        file="myapp:latest",
        repository=docker_hub.tosca_name,
    )
