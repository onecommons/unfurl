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
hello_world_container.Standard_default_inputs = dict(
    configuration=dict(command=["echo", "hello world"], detach=False, output_logs=True)
)
