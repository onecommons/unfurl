import unfurl
import tosca
from tosca import gb
class ContainerService(tosca.nodes.Root):
    image: str
    url: str
    mem_size: tosca.Size = tosca.CONSTRAINED
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

        cls.set_to_property_source(cls.hosting, cls.backend_url)
        # the following are equivalent:
        # cls.set_to_property_source(cls.hosting, "backend_url")
        # cls.hosting = cls.backend_url  # type: ignore

class App(tosca.nodes.Root):
    container: ContainerService = ContainerService(
        "container_service", image="myimage:latest", url="http://localhost:8000"
    )
    proxy: ProxyContainerHost

    @classmethod
    def _class_init(cls) -> None:
        # the proxy's backend is set to the container's url
        cls.proxy.backend_url = cls.container.url
        cls.proxy.hosting.name = "app"
        tosca.in_range(2*gb, 20*gb).apply_constraint(cls.proxy.hosting.mem_size)
        # same as:
        # cls.proxy.hosting.mem_size = tosca.in_range(2*gb, 20*gb)

topology = App("myapp", proxy=ProxyContainerHost())
