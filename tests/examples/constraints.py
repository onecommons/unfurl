import unfurl
import tosca
from tosca import gb
from typing_extensions import reveal_type
class ContainerService(tosca.nodes.Root):
    image: str
    url: str
    mem_size: tosca.Size
    name: str = tosca.CONSTRAINED

class ContainerHost(tosca.nodes.Root):
    hosting: ContainerService

class Proxy(tosca.nodes.Root):
    backend_url: str = tosca.CONSTRAINED
    "URL to proxy"
    endpoint: str = tosca.Attribute()
    "Public URL"
class ProxyContainerHost(Proxy, ContainerHost):
    # container hosts that proxies the container service it hosts
    hosting: ContainerService = tosca.CONSTRAINED

    @classmethod
    def _class_init(cls) -> None:
        # the backend is the container services url
        cls.backend_url = cls.hosting.url
        # set the hosting to the source of the backend
        cls.set_to_property_source(cls.hosting, cls.backend_url)
        # now you can set either "hosting" or the "backend_url" on the template and the other will be set

        # note: the following are equivalent:
        # cls.set_to_property_source(cls.hosting, "backend_url")
        # cls.hosting = cls.backend_url  # type: ignore

class App(tosca.nodes.Root):
    container: ContainerService = ContainerService(
        "container_service", image="myimage:latest", url="http://localhost:8000", mem_size=1*gb
    )
    proxy: ProxyContainerHost

    @classmethod
    def _class_init(cls) -> None:
        # the proxy's backend_url is set to the container's url
        # by adding node_filter property constraint on the proxy requirement
        cls.proxy.backend_url = cls.container.url
        # name is by adding a node_filter requirements constraint with a property constraint on the proxy requirement
        cls.proxy.hosting.name = "app"
        tosca.in_range(1*gb*2, 20*gb).apply_constraint(cls.proxy.hosting.mem_size)
        # the following is equivalent, but has a static type error:
        # cls.proxy.hosting.mem_size = tosca.in_range(2*gb, 20*gb)

topology = App("myapp", proxy=ProxyContainerHost())
