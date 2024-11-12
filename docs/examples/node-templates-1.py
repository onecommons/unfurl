import unfurl
from typing import Sequence
import tosca
from tosca import Eval, Property
import unfurl.configurators.shell


class NodesNginx(tosca.nodes.WebServer):
    port: int

    def create(self, **kw):
        return unfurl.configurators.shell.ShellConfigurator(
            command="scripts/install-nginx.sh",
            inputs={
                "process": {
                    "env": {
                        "port": Eval({"get_property": ["SELF", "port"]})
                    }
                }
            },
        )

    def start(self, **kw):
        return unfurl.configurators.shell.ShellConfigurator(
            command="scripts/start-nginx.sh",
        )


vm = tosca.nodes.Compute("vm")
vm.ip = "192.168.0.11"

nginx = NodesNginx("nginx", port=80)

nginx.host = tosca.relationships.HostedOn()
nginx.host.target = vm

__all__ = ["NodesNginx", "vm", "nginx"]

