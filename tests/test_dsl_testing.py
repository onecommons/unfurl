import pytest
import unfurl
import tosca
from unfurl.dsl import runtime_test
from unfurl.configurators.terraform import tfoutput, tfvar
from typing import Optional


class Service(tosca.nodes.Root):
    host: str = tosca.Attribute()

    url_scheme: str = "https"

    url: str = tosca.Property(
        default=tosca.Eval("{{ SELF.url_scheme }}://{{SELF.host }}"),
    )

    connects_to: Optional["Service"] = None


def test_runtime_test():
    tosca.global_state.mode = "spec"

    # Instantiate the objects you want to test in a Namespace
    # (so you can pass it to runtime_test() below)
    class test(tosca.Namespace):
        service = Service()

    assert tosca.global_state.mode == "spec"
    # start in spec mode, where properties always return eval expressions
    assert test.service.url == {"eval": "::test_service::url"}

    assert test.service.get_field("url") == Service.get_field("url")
    # Service.url is a FieldProjection
    assert test.service.get_field("url") == Service.url.field  # type: ignore

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


def test_options():
    class MyOption(tosca.Options):
        def __init__(self, val: str):
            super().__init__(dict(my_option=val))

    p = tosca.Property(options=tfvar)
    assert p.metadata == dict(tfvar=True)
    tosca.Attribute(options=tfoutput)
    with pytest.raises(ValueError):
        tosca.Attribute(options=tfvar)
    with pytest.raises(ValueError):
        tosca.Property(options=tfoutput)
    field = tosca.Property(options=tfvar | MyOption("foo"))
    assert field.metadata == dict(tfvar=True, my_option="foo")


@pytest.mark.parametrize(
    "requirement,expected",
    [
        (Service.connects_to, None),
        ("connects_to", Service),
        (Service.connects_to, Service),
    ],
)
def test_find_required_by(requirement, expected):
    tosca.global_state.mode = "spec"

    # Instantiate the objects you want to test in a Namespace
    # (so you can pass it to runtime_test() below)
    class test(tosca.Namespace):
        connection = Service()
        service = Service(connects_to=connection)

    assert test.connection._name == "test_connection"  # XXX should be test.connection
    assert test.connection.find_required_by(requirement, expected) == tosca.Eval(
        {"eval": "::test_connection::.sources::connects_to"}
    )
    assert test.connection.find_all_required_by(requirement, expected) == tosca.Eval(
        {"eval": "::test_connection::.sources::connects_to", "foreach": "$true"}
    )

    with pytest.raises(TypeError):
        test.connection.find_required_by(Service.connects_to, tosca.nodes.Compute)

    with pytest.raises(TypeError):
        test.connection.find_all_required_by(Service.connects_to, tosca.nodes.Compute)

    tosca.global_state.mode = "yaml"
    assert test.service.connects_to == test.connection

    topology = runtime_test(test)

    assert topology.service.connects_to == topology.connection
    assert (
        topology.connection.find_required_by(requirement, expected) == topology.service
    )
    assert (
        topology.connection.find_all_required_by(requirement, expected) == [topology.service]
    )

    with pytest.raises(TypeError):
        topology.connection.find_required_by(Service.connects_to, tosca.nodes.Compute)

    with pytest.raises(TypeError):
        topology.connection.find_all_required_by(Service.connects_to, tosca.nodes.Compute)
