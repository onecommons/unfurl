from unfurl.configurators.templates.docker import (
    unfurl_nodes_Container_Application_Docker,
)
import tosca

hello_world_container = unfurl_nodes_Container_Application_Docker(
    "hello-world-container",
    image=tosca.artifacts.DeploymentImageContainerDocker(
        "image",
        file="busybox",
    ),
)
