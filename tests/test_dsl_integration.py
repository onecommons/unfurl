import os
import pytest
import unfurl
import tosca
from tosca import Size, MB, EvalData, operation
from unfurl.eval import Ref
from unfurl.job import JobOptions
from unfurl.logs import is_sensitive, getLogger
from unfurl.support import Status
from unfurl.testing import runtime_test, create_runner
from unfurl.tosca_plugins import expr, functions
from typing import Optional, Type
from unfurl.util import UnfurlError
import tosca._tosca
from unfurl.tosca_plugins.expr import tfvar, tfoutput, find_connection
from typing import Any
from unfurl.configurators import DoneDict, TemplateConfigurator, TemplateInputs
from unfurl.tosca_plugins.k8s import (
    unfurl_nodes_K8sCluster,
    unfurl_relationships_ConnectsTo_K8sCluster,
)


class Service(tosca.nodes.Root):
    host: str = tosca.Attribute()

    url_scheme: str = "https"

    url: str = tosca.Property(
        default=tosca.Eval("{{ SELF.url_scheme }}://{{SELF.host }}"),
    )

    not_required: Optional[str] = None

    parent: "Service" = tosca.find_required_by("connects_to")

    connects_to: Optional["Service"] = tosca.Requirement(
        default=None, relationship=unfurl.relationships.Configures
    )


class MyService(Service):
    def get_scheme(self):
        return "web+" + expr.super()["url_scheme"]

    url_scheme: str = tosca.Eval(get_scheme)


def test_runtime_test():
    tosca.global_state.mode = "parse"

    # Instantiate the objects you want to test in a Namespace
    # (so you can pass it to runtime_test() below)
    class test(tosca.Namespace):
        service = Service()

    assert tosca.global_state.mode == "parse"
    # start in parse mode, where properties always return eval expressions
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

    p = tosca.Property(options=expr.tfvar)
    assert p.metadata == dict(tfvar=True)
    tosca.Attribute(options=expr.tfoutput)
    with pytest.raises(ValueError):
        tosca.Attribute(options=expr.tfvar)
    with pytest.raises(ValueError):
        tosca.Property(options=expr.tfoutput)
    field = tosca.Property(options=expr.tfvar | MyOption("foo"))
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
    tosca.global_state.mode = "parse"

    # Instantiate the objects you want to test in a Namespace
    # (so you can pass it to runtime_test() below)
    class test(tosca.Namespace):
        connection = Service()
        service = Service(connects_to=connection)

    assert test.connection._name == "test.connection"
    assert test.connection.find_required_by(requirement, expected_type) == tosca.Eval({
        "eval": "::test.connection::.sources::connects_to"
    })
    assert test.connection.find_all_required_by(
        requirement, expected_type
    ) == tosca.Eval({
        "eval": "::test.connection::.sources::connects_to",
        "foreach": "$true",
    })

    if expected_type:
        assert test.connection.find_required_by(
            requirement, expected_type
        ).url == tosca.Eval({"eval": "::test.connection::.sources::connects_to::url"})

    with pytest.raises(TypeError):
        test.connection.find_required_by(Service.connects_to, tosca.nodes.Compute)

    with pytest.raises(TypeError):
        test.connection.find_all_required_by(Service.connects_to, tosca.nodes.Compute)

    assert test.connection.find_configured_by(Service.url) == tosca.Eval({
        "eval": "::test.connection::.configured_by::url"
    })

    tosca.global_state.mode = "yaml"
    assert test.service.connects_to == test.connection

    topology = runtime_test(test)

    assert topology.service.connects_to == topology.connection
    assert topology.connection.connects_to == None
    assert (
        topology.connection.find_required_by(requirement, expected_type)
        == topology.service
    )
    assert topology.connection.find_all_required_by(requirement, expected_type) == [
        topology.service
    ]

    assert topology.service.find_all_required_by(requirement, expected_type) == []

    assert topology.connection.parent == topology.service
    # print ( topology.connection._instance.template.sources )

    with pytest.raises(TypeError):
        topology.connection.find_required_by(Service.connects_to, tosca.nodes.Compute)

    with pytest.raises(TypeError):
        topology.connection.find_all_required_by(
            Service.connects_to, tosca.nodes.Compute
        )

    topology.service.host = "example.com"
    assert topology.connection.find_configured_by(Service.url) == "https://example.com"


def test_hosted_on():
    tosca.global_state.mode = "parse"

    class test(tosca.Namespace):
        server = tosca.nodes.Compute(
            os=tosca.capabilities.OperatingSystem(
                architecture="x86_64",
                type="linux",
            ),
        )

        software = tosca.nodes.SoftwareComponent(host=[server])
        setattr(
            software,
            "architecture",
            tosca.find_hosted_on(tosca.nodes.Compute.os).architecture,
        )

    assert test.software.find_hosted_on(
        tosca.nodes.Compute.os
    ).architecture == tosca.Eval({
        "eval": "::test.software::.hosted_on::.capabilities::[.name=os]::architecture"
    })
    assert test.software.architecture == {
        "eval": ".hosted_on::.capabilities::[.name=os]::architecture"
    }

    topology = runtime_test(test)
    assert (
        topology.software.find_hosted_on(tosca.nodes.Compute.os).architecture
        == "x86_64"
    )
    assert topology.software.architecture == "x86_64"


def validate_pw(password: str) -> bool:
    return len(password) > 4


def test_expressions():
    tosca.global_state.mode = "parse"

    class Inputs(tosca.TopologyInputs):
        domain: str

    class Test(tosca.nodes.Root):
        url: str = expr.uri()
        path1: str = expr.get_dir(None, "src")
        default_expr: str = expr.fallback(None, "foo")
        or_expr: str = expr.or_expr(default_expr, "ignored")
        label: str = functions.to_dns_label(expr.get_input("missing", "fo!o"))
        password: str = tosca.Property(
            options=expr.sensitive | expr.validate(validate_pw), default="default"
        )

    class test(tosca.Namespace):
        service = Service()
        test_node = Test()
        myService = MyService()

    assert Inputs.domain == tosca.EvalData({"get_input": "domain"})

    topology = runtime_test(test)
    assert expr.get_instance(topology.test_node).status == Status.ok
    expr.get_instance(topology.test_node).local_status = Status.error
    assert expr.get_instance(topology.test_node).status == Status.error
    assert topology.service.url_scheme == "https"
    assert topology.myService.url_scheme == "web+https"
    assert expr.get_env("MISSING", "default") == "default"
    assert not expr.has_env("MISSING")
    assert expr.get_env("PATH")
    assert expr.get_nodes_of_type(Service) == [topology.service, topology.myService]
    assert expr.get_input("MISSING", "default") == "default"
    with pytest.raises(UnfurlError):
        input: str = expr.get_input("MISSING")
    assert expr.get_dir(topology.service, "src").get() == os.path.dirname(__file__)
    # XXX assert topology.test_node.path1 == os.path.dirname(__file__)
    assert (
        expr.abspath(topology.service, "test_dsl_integration.py", "src").get()
        == __file__
    )
    assert expr.uri(None) != topology.test_node.url
    assert expr.uri(topology.test_node) == topology.test_node.url
    assert functions.to_label("fo!oo", replace="_") == "fo_oo"
    assert (
        expr.template(topology.test_node, contents="{%if 1 %}{{SELF.url}}{%endif%}")
        == "#::test.test_node"
    )
    assert topology.test_node.default_expr == "foo"
    assert topology.test_node.or_expr == "foo"
    assert topology.test_node.label == "fo--o"
    assert (
        "to_dns_label"
        in topology.test_node._instance.attributes.defs["label"].default["eval"]
    )
    assert is_sensitive(topology.test_node.password)
    with pytest.raises(UnfurlError, match=r"validation failed for"):
        topology.test_node.password = ""
    # XXX test:
    # "if_expr", and_expr
    # "lookup",
    # "to_env",
    # "get_ensemble_metadata",
    # "not_",
    # "as_bool",
    # tempfile


from tosca import MB, GB, mb, gb, Size
import math


@expr.runtime_func
def calc_size(size1: Size, size2: Size) -> Size:
    if size1 is None or size2 is None:
        return None
    # print("calc_size", size1, size2, max(size1, size2).ceil(GB))
    return GB.as_int(max(size1, size2)) * GB


@pytest.mark.parametrize("safe_mode", [True, False])
def test_units(safe_mode):
    tosca.global_state.safe_mode = safe_mode
    tosca.global_state.mode = "parse"

    g = 2 * gb
    foo: float = float(20 * g)
    bar: Size = g * 2.0
    bar: Size = g * 2
    baz: Size = 20.0 * g
    one_mb = 1 * mb
    assert abs(-one_mb) == one_mb
    assert abs(one_mb) == +one_mb
    assert hash(one_mb)
    assert bool(0 * MB) == bool(0.0)
    assert one_mb.value == 1000000.0
    assert str(one_mb) == "1 MB"
    assert repr(one_mb) == "1.0*MB"
    assert one_mb.as_unit == 1.0
    assert one_mb.to_yaml() == "1 MB"
    assert str(one_mb * GB) == "0.001 GB"
    with pytest.raises(TypeError, match="Hz"):
        str(one_mb * tosca.HZ)

    mem_size: Size = 4000 * MB + 10 * GB
    assert mem_size == 14 * GB
    assert mem_size == 14000000000

    class Topology(tosca.Namespace):
        host = tosca.capabilities.Compute(
            num_cpus=1,
            disk_size=10 * GB,
            mem_size=mem_size,
        )

        class Test(tosca.nodes.Root):
            mem_size: Size = tosca.Attribute()
            host: tosca.capabilities.Compute

        test = Test(host=host)

        assert host.mem_size
        assert host.mem_size == 14 * GB
        compute = tosca.capabilities.Compute(
            num_cpus=1,
            disk_size=host.mem_size * 2,
            mem_size=test.mem_size + test.mem_size,
        )

        assert compute.disk_size
        assert compute.mem_size
        assert isinstance(compute.disk_size * 2, Size)
        # compute.mem_size depends on Test.mem_size which is a tosca attribute so it needs to be EvalData
        assert isinstance(compute.mem_size, tosca.EvalData)
        assert isinstance(compute.mem_size * 2, tosca.EvalData)
        type_pun: Size = calc_size(compute.disk_size, compute.mem_size) + 4 * GB
        assert isinstance(type_pun, tosca.EvalData)
        if not tosca.global_state.safe_mode:
            assert calc_size(compute.disk_size, compute.disk_size) == 28 * GB
        test2 = Test(
            host=tosca.capabilities.Compute(
                mem_size=calc_size(compute.disk_size, compute.mem_size)
            )
        )
        test3 = Test(host=compute)

    topology, runner = create_runner(Topology)
    # make sure we can serialize this
    str_io = runner.manifest.manifest.save()
    # print(str_io.getvalue())
    assert (
        """
        eval:
          computed:
          - tests.test_dsl_integration:calc_size
          - eval:
              scalar: 28000 MB
          - eval: ::Topology.test3::.capabilities::[.name=host]::mem_size
    """.replace(" ", "")
        in str_io.getvalue().replace(" ", "")
    )
    calcd = runner.manifest.rootResource.query(
        "::Topology.test::.capabilities::[.name=host]::mem_size", trace=0
    )
    assert calcd == "14000 MB"
    topology.test.mem_size = 2 * MB  # set attribute value so expression resolves
    assert topology.test.mem_size == 2 * MB
    assert topology.test3.host.mem_size == 4 * MB
    expr = EvalData({
        "eval": "::Topology.test3::.capabilities::[.name=host]::.owner::mem_size"
    })
    assert Topology.test3.host.from_owner(Topology.Test.mem_size) == expr
    assert (
        runner.manifest.rootResource.query(
            "::Topology.test::.capabilities::[.name=host]::.owner::mem_size", trace=2
        )
        == 2 * MB
    )
    assert topology.test.host.from_owner(Topology.Test.mem_size) == 2 * MB
    tosca.global_state.safe_mode = False
    result = Ref(topology.type_pun.expr).resolve_one(
        tosca.global_state.context.copy(trace=0)
    )
    assert result == 32 * GB


def test_find_connection():
    class Topology(tosca.Namespace):
        cluster = unfurl_nodes_K8sCluster()
        cluster_connection = unfurl_relationships_ConnectsTo_K8sCluster(
            _default_for=cluster,
            api_server="https://127.0.0.1",
            cluster_ca_certificate_file="cert.crt",
        )

        class ExampleTerraformManagedResource(tosca.nodes.Root):
            @operation(apply_to=["Standard.configure", "Standard.delete"])
            def default(self, **kw: Any):
                connection = find_connection(
                    self.cluster, unfurl_relationships_ConnectsTo_K8sCluster
                )
                if connection:
                    tfvars = dict(
                        cert=connection.cluster_ca_certificate_file,
                        api_server=connection.api_server,
                    )
                else:
                    assert False
                # getLogger(__name__).debug("tfvars %s", tfvars)
                return TemplateConfigurator(
                    TemplateInputs(done=DoneDict(outputs=tfvars))
                )

            cluster: unfurl_nodes_K8sCluster

        test = ExampleTerraformManagedResource(cluster=cluster)
        assert cluster_connection._default_for is cluster

    topology, runner = create_runner(Topology)
    runner.job = None
    job = runner.run(JobOptions(skip_save=True, check=True))
    assert job
    assert len(job.workDone) == 2, len(job.workDone)
    task = list(job.workDone.values())[1]
    assert runner.manifest.rootResource.template.relationship_templates
    assert (
        task.outputs
        and task.outputs["cert"] == "cert.crt"
        and task.outputs["api_server"] == "https://127.0.0.1"
    )
