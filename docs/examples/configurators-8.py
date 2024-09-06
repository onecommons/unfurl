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


__all__ = ["example_com_zone", "test_app"]
