import unfurl
from unfurl.configurators.templates.docker import unfurl_datatypes_DockerContainer
from tosca import Size, TopologyInputs, gb, mb
from tosca_repositories import std

class Inputs(TopologyInputs):
    disk_size: Size = 10 * gb
    mem_size: Size = 2048 * mb

db = std.PostgresDB()

host = std.ContainerHost()

container = std.ContainerService(
    environment=unfurl.datatypes.EnvironmentVariables(
        DBASE=db.url, URL=std.SQLWebApp.url
    ),
    host_requirement=host,
    container=unfurl_datatypes_DockerContainer(
        image="registry.gitlab.com/gitlab-org/project-templates/express/main:latest",
        ports=["5000:5000"],
        deploy={"resources": {"limits": {"memory": Inputs.mem_size}}},
    ),
)

__root__ = std.SQLWebApp(container=container, db=db, subdomain="myapp")
