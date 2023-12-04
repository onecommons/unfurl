import unfurl
import tosca
from unfurl.dsl import runtime_test


class Service(tosca.nodes.Root):
    host: str = tosca.Attribute()

    url_scheme: str = "https"

    url: str = tosca.Property(
        default=tosca.Eval("{{ SELF.url_scheme }}://{{SELF.host }}"),
    )


def test_runtime_test():
    tosca.global_state.mode = "spec"
    # Instantiate the objects you want to test in a Namespace
    # (so you can pass it to runtime_test() below)
    class test(tosca.Namespace):
        service = Service()

    assert tosca.global_state.mode == "spec"
    # start in spec mode, where properties always return eval expressions
    assert test.service.url == {"eval": "::test_service::url"}

    # switch to "runtime" mode, which returns properties' values but computed expressions still are unevaluated
    tosca.global_state.mode = "runtime"
    assert test.service.url == "{{ SELF.url_scheme }}://{{SELF.host }}"

    # runtime_test() builds an ensemble and evaluates the namespace into a topology of resource instances
    topology = runtime_test(test)
    # now properties evaluate to real values and are ready for real testing

    assert topology.service.url == "https://None"

    # assign a value to an attribute
    topology.service.host = "example.com"
    assert topology.service.host == "example.com"
    assert topology.service.url == "https://example.com"
    assert topology.service.host == "example.com"
