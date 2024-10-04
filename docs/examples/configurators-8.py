import unfurl
import tosca
from tosca import Eval

from unfurl.configurators.templates.dns import unfurl_nodes_DNSZone, unfurl_relationships_DNSRecords

example_com_zone = unfurl_nodes_DNSZone(
    "example_com_zone",
    name="example.com.",
    provider={"class": "octodns.provider.route53.Route53Provider"},
)

test_app = tosca.nodes.WebServer(
    "test_app",
    host=[tosca.find_node("compute")],
)
test_app.dns = unfurl_relationships_DNSRecords(
    "",
    records=Eval(
        {
            "www": {
                "type": "A",
                "value": {
                    "eval": ".source::.requirements::[.name=host]::.target::public_address"
                },
            }
        }
    ),
)[example_com_zone]
