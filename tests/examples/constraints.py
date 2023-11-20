import unfurl
import tosca

class ContainerService(tosca.nodes.Root):
    image: str
    url: str

class ContainerHost(tosca.nodes.Root):
    hosting: ContainerService

class Proxy(tosca.nodes.Root):
    backend_url: str = tosca.Eval()
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
        cls.set_source(cls.hosting, cls.backend_url)

class App(tosca.nodes.Root):
    container: ContainerService = ContainerService(
        "container_service", image="myimage:latest", url="http://localhost:8000"
    )
    proxy: Proxy

    @classmethod
    def _class_init(cls) -> None:
        # the proxy's backend is set to the container's url
        cls.proxy.backend_url = cls.container.url

topology = App("myapp", proxy=ProxyContainerHost())
