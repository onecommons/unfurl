from unfurl.configurators.templates.dns import unfurl_nodes_DNSZone
import tosca

example_com_zone = unfurl_nodes_DNSZone(
    "example_com_zone",
    name="example.com.",
    provider={"class": "octodns.provider.route53.Route53Provider"},
)

test_app = tosca.nodes.WebServer(
    "test_app",
    host=[tosca.find_node("compute")],
)
test_app.dns = example_com_zone
