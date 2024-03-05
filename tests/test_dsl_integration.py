import os
import pytest
import unfurl
import tosca
from unfurl.testing import runtime_test
from unfurl.tosca_plugins import expr,functions
from unfurl.configurators.terraform import tfoutput, tfvar
from typing import Optional, Type
from unfurl.util import UnfurlError


class Service(tosca.nodes.Root):
    host: str = tosca.Attribute()

    url_scheme: str = "https"

    url: str = tosca.Property(
        default=tosca.Eval("{{ SELF.url_scheme }}://{{SELF.host }}"),
    )

    not_required: Optional[str] = None

    parent: "Service" = tosca.find_required_by("connects_to")

    connects_to: Optional["Service"] = tosca.Requirement(default=None, relationship=unfurl.relationships.Configures)


def test_runtime_test():
    tosca.global_state.mode = "spec"

    # Instantiate the objects you want to test in a Namespace
    # (so you can pass it to runtime_test() below)
    class test(tosca.Namespace):
        service = Service()

    assert tosca.global_state.mode == "spec"
    # start in spec mode, where properties always return eval expressions
    assert test.service.url == {"eval": "::test.service::url"}

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
    assert topology.service.not_required == None


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
    "requirement,expected_type",
    [
        (Service.connects_to, None),
        ("connects_to", Service),
        (Service.connects_to, Service),
    ],
)
def test_find_required_by(requirement, expected_type: Optional[Type[Service]]):
    tosca.global_state.mode = "spec"

    # Instantiate the objects you want to test in a Namespace
    # (so you can pass it to runtime_test() below)
    class test(tosca.Namespace):
        connection = Service()
        service = Service(connects_to=connection)

    assert test.connection._name == "test.connection"
    assert test.connection.find_required_by(requirement, expected_type) == tosca.Eval(
        {"eval": "::test.connection::.sources::connects_to"}
    )
    assert test.connection.find_all_required_by(requirement, expected_type) == tosca.Eval(
        {"eval": "::test.connection::.sources::connects_to", "foreach": "$true"}
    )

    if expected_type:
        assert test.connection.find_required_by(requirement, expected_type).url == tosca.Eval(
            {"eval": "::test.connection::.sources::connects_to::url"}
        )

    with pytest.raises(TypeError):
        test.connection.find_required_by(Service.connects_to, tosca.nodes.Compute)

    with pytest.raises(TypeError):
        test.connection.find_all_required_by(Service.connects_to, tosca.nodes.Compute)

    assert test.connection.find_configured_by(Service.url) == tosca.Eval(
            {"eval": "::test.connection::.configured_by::url"}
        )

    tosca.global_state.mode = "yaml"
    assert test.service.connects_to == test.connection

    topology = runtime_test(test)

    assert topology.service.connects_to == topology.connection
    assert topology.connection.connects_to == None
    assert (
        topology.connection.find_required_by(requirement, expected_type) == topology.service
    )
    assert (
        topology.connection.find_all_required_by(requirement, expected_type) == [topology.service]
    )

    assert (
        topology.service.find_all_required_by(requirement, expected_type) == []
    )

    # XXX this is broken because the node_filter match is evaluated too early and so .sources is empty
    # assert topology.connection.parent == topology.service
    # print ( topology.connection._instance.template.sources )
    assert topology.service.parent == None

    with pytest.raises(TypeError):
        topology.connection.find_required_by(Service.connects_to, tosca.nodes.Compute)

    with pytest.raises(TypeError):
        topology.connection.find_all_required_by(Service.connects_to, tosca.nodes.Compute)
    
    topology.service.host = 'example.com'
    assert topology.connection.find_configured_by(Service.url) == "https://example.com"

def test_hosted_on():
    tosca.global_state.mode = "spec"
    class test(tosca.Namespace):
        server = tosca.nodes.Compute(
            os=tosca.capabilities.OperatingSystem(
                architecture="x86_64",
                type="linux",
            ),
        )

        software = tosca.nodes.SoftwareComponent(host=[server])
        setattr(software, "architecture", tosca.find_hosted_on(tosca.nodes.Compute.os).architecture)

    assert test.software.find_hosted_on(tosca.nodes.Compute.os).architecture == tosca.Eval(
        {"eval": "::test.software::.hosted_on::.capabilities::[.name=os]::architecture"}
    )
    assert test.software.architecture ==  {'eval': '.hosted_on::.capabilities::[.name=os]::architecture' }

    topology = runtime_test(test)
    assert topology.software.find_hosted_on(tosca.nodes.Compute.os).architecture == "x86_64"
    assert topology.software.architecture == "x86_64"


def test_expressions():
    tosca.global_state.mode = "spec"
    class Test(tosca.nodes.Root):
        url: str = expr.uri()
        path1: str = expr.get_dir(None, "src")
        default_expr: str = expr.fallback(None, "foo")
        or_expr: str = expr.or_expr(default_expr, "ignored")
    class test(tosca.Namespace):
        service = Service()
        test_node = Test()

    topology = runtime_test(test)
    assert expr.get_env("MISSING", "default") == "default"
    assert not expr.has_env("MISSING")
    assert expr.get_env("PATH")
    assert expr.get_nodes_of_type(Service) == [topology.service]
    assert expr.get_input("MISSING", "default") == "default"
    with pytest.raises(UnfurlError):
        assert expr.get_input("MISSING")
    assert expr.get_dir(topology.service, "src").get() == os.path.dirname(__file__)
    # XXX assert topology.test_node.path1 == os.path.dirname(__file__)
    assert expr.abspath(topology.service, "test_dsl_integration.py", "src").get() == __file__
    assert expr.uri(None) != topology.test_node.url
    assert expr.uri(topology.test_node) == topology.test_node.url
    assert functions.to_label("fo!oo", replace='_') == 'fo_oo'
    assert expr.template(topology.test_node, contents="{%if 1 %}{{SELF.url}}{%endif%}") == "#::test.test_node"
    assert topology.test_node.default_expr == "foo"
    assert topology.test_node.or_expr == "foo"
    # XXX test:
    # "if_expr", and_expr
    # "lookup",
    # "to_env",
    # "get_ensemble_metadata",
    # "negate",
    # "as_bool",
    # tempfile
