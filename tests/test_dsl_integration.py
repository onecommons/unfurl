import logging
import os
import sys
import datetime
import importlib
import pprint
import pytest
import unfurl
import tosca
from tosca import List, Size, MB, EvalData, operation
from unfurl.eval import Ref, RefContext, UnfurlEvalError
from unfurl.job import JobOptions, Runner
from unfurl.yamlmanifest import YamlManifest
from unfurl.logs import is_sensitive, getLogger
from unfurl.result import serialize_value
from unfurl.support import Status
from unfurl.testing import runtime_test, create_runner, namespace2manifest
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
from unfurl.dsl import is_python_file_newer
from tosca import MB, GB, mb, gb, Size, kib, KB
import math

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
    field2 = tosca.Property(options=expr.tfvar)
    assert field2.metadata == dict(tfvar=True)
    field3 = tosca.Property(options=expr.sensitive | expr.set_inert())
    assert field3.metadata == dict(inert=True, sensitive=True)


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
    assert test.software.architecture == {"eval": "::test.software::architecture"}

    topology = runtime_test(test)
    assert (
        topology.software.find_hosted_on(tosca.nodes.Compute.os).architecture
        == "x86_64"
    )


def validate_pw(password: str) -> bool:
    return len(password) > 4


expressions_yaml = {
    "tosca_definitions_version": "tosca_simple_unfurl_1_0_0",
    "topology_template": {
        "node_templates": {
            "test.service": {
                "type": "Service",
                "metadata": {"module": "tests.test_dsl_integration"},
            },
            "test.test_node": {
                "type": "Test",
                "metadata": {"module": "tests.test_dsl_integration"},
            },
            "test.myService": {
                "type": "MyService",
                "metadata": {"module": "tests.test_dsl_integration"},
            },
        },
        "inputs": {"domain": {"type": "string"}},
    },
    "input_values": {"domain": "example.com"},
    "node_types": {
        "Service": {
            "derived_from": "tosca.nodes.Root",
            "attributes": {"host": {"type": "string"}},
            "properties": {
                "url_scheme": {"type": "string", "default": "https"},
                "url": {
                    "type": "string",
                    "default": "{{ SELF.url_scheme }}://{{SELF.host }}",
                },
                "not_required": {"type": "string", "required": False},
            },
            "requirements": [
                {
                    "parent": {
                        "node": "Service",
                        "node_filter": {"match": [{"eval": ".sources::connects_to"}]},
                    }
                },
                {
                    "connects_to": {
                        "relationship": "unfurl.relationships.Configures",
                        "node": "Service",
                        "occurrences": [0, 1],
                    }
                },
            ],
        },
        "Test": {
            "derived_from": "tosca.nodes.Root",
            "properties": {
                "url": {"type": "string", "default": {"eval": ".uri"}},
                "path1": {
                    "type": "string",
                    "default": {"eval": {"get_dir": ["src", False]}},
                },
                "default_expr": {
                    "type": "string",
                    "default": {"eval": {"or": [None, "foo"], "map_value": 1}},
                },
                "or_expr": {
                    "type": "string",
                    "default": {
                        "eval": {
                            "or": [
                                {"eval": {"or": [None, "foo"], "map_value": 1}},
                                "ignored",
                            ],
                            "map_value": 1,
                        }
                    },
                },
                "label": {
                    "type": "string",
                    "default": {
                        "eval": {
                            "inert": {
                                "eval": {
                                    "allowed": "[a-zA-Z0-9-]",
                                    "start": "[a-zA-Z]",
                                    "replace": "--",
                                    "case": "lower",
                                    "end": "[a-zA-Z0-9]",
                                    "max": 63,
                                    "to_dns_label": {"get_input": ["missing", "fo!o"]},
                                }
                            }
                        }
                    },
                },
                "password": {
                    "type": "string",
                    "default": "default",
                    "metadata": {
                        "sensitive": True,
                        "validation": {
                            "eval": {
                                "validate": "tests.test_dsl_integration:validate_pw"
                            }
                        },
                    },
                },
            },
        },
        "MyService": {
            "derived_from": "Service",
            "properties": {
                "url_scheme": {
                    "type": "string",
                    "default": {
                        "eval": {
                            "computed": "tests.test_dsl_integration:MyService.get_scheme"
                        }
                    },
                }
            },
        },
    },
}


def test_expressions():
    tosca.global_state.mode = "parse"

    class Inputs(tosca.TopologyInputs):
        domain: str

    class Test(tosca.nodes.Root):
        url: str = expr.uri()
        path1: str = expr.get_dir(None, "src")
        default_expr: str = expr.fallback(None, "foo")
        or_expr: str = expr.or_expr(default_expr, "ignored")
        label: str = expr.inert(
            functions.to_dns_label(expr.get_input("missing", "fo!o"))
        )
        password: str = tosca.Property(
            options=expr.sensitive | expr.validate(validate_pw), default="default"
        )

    class test(tosca.Namespace):
        service = Service()
        test_node = Test()
        myService = MyService()
        inputs = Inputs(domain="example.com")

    assert Inputs.domain == tosca.EvalData({"get_input": "domain"})
    assert test.inputs.domain == "example.com"

    topology = runtime_test(test)
    assert topology._yaml == expressions_yaml
    assert tosca.global_state.mode == "runtime"

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
    assert topology.inputs.domain == "example.com"

    dir_value = expr.get_dir(topology.service, "src")
    assert dir_value.get() == os.path.dirname(__file__)
    assert serialize_value(dir_value) == {"eval": {"abspath": ["tests", "self"]}}
    # XXX assert topology.test_node.path1 == os.path.dirname(__file__)
    fp = expr.abspath(topology.service, "test_dsl_integration.py", "src")
    assert fp.get() == __file__
    assert serialize_value(fp) == {
        "eval": {"abspath": ["tests/test_dsl_integration.py", "self"]}
    }

    assert expr.uri(None) != topology.test_node.url
    assert expr.uri(topology.test_node) == topology.test_node.url

    assert functions.to_label("fo!oo", replace="_") == "fo_oo"
    assert (
        expr.template(
            topology.test_node,
            contents="{%if 1 %}{{ a }}{%endif%}",
            vars=dict(a="{{ SELF.url }}"),
        )
        == "#:::test.test_node"
    )
    assert (
        expr.template(
            topology.test_node,
            contents="{%if 1 %}{{ a }}{%endif%}",
            vars=dict(a=topology.test_node.url),
        )
        == "#:::test.test_node"
    )
    assert topology.test_node.default_expr == "foo"
    assert topology.test_node.or_expr == "foo"
    assert topology.test_node.label == "fo--o"
    assert (
        "to_dns_label"
        in topology.test_node._instance.attributes.defs["label"].default["eval"][
            "inert"
        ]["eval"]
    )

    assert is_sensitive(topology.test_node.password)
    with pytest.raises(UnfurlError, match=r"validation failed for"):
        topology.test_node.password = ""

    resolved = "ghcr.io/onecommons/unfurl@sha256:acd6c243b16145778f8ed96b7b3b7d26b211664114b1e8dbc5537902cc456afc"
    assert (
        expr.get_artifact(None, "ghcr.io/onecommons/unfurl:v1.1.0").with_digest
        == resolved
    )
    image = expr.container_image("ghcr.io/onecommons/unfurl:v1.1.0")
    assert image
    assert image.get() == "ghcr.io/onecommons/unfurl:v1.1.0"

    with tosca.set_evaluation_mode("parse"):
        evaldata = expr.get_artifact(
            None, "ghcr.io/onecommons/unfurl:v1.1.0"
        ).with_digest
        assert evaldata == {
            "eval": {
                "get_artifact": [None, "ghcr.io/onecommons/unfurl:v1.1.0", None, None]
            },
            "select": "with_digest",
        }
        assert Ref(evaldata.expr).resolve_one(tosca.global_state_context()) == resolved
        evaldata2 = expr.container_image("ghcr.io/onecommons/unfurl:v1.1.0")
        # arg is a string so it is evaluated eagerly
        assert evaldata2.get() == image.get()

    # XXX test:
    # "if_expr", and_expr
    # "lookup",
    # "to_env",
    # "get_ensemble_metadata",
    # "not_",
    # "as_bool",
    # tempfile


@expr.runtime_func
def calc_size(size1: Size, size2: Size) -> Size:
    if size1 is None or size2 is None:
        return None
    # print("calc_size", size1, size2, type(size1)) # max(size1, size2).ceil(GB))
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
    assert str(one_mb) == "1MB"
    assert repr(one_mb) == "1.0*MB"
    assert one_mb.as_unit == 1.0
    assert one_mb.to_yaml() == "1MB"
    assert str(one_mb * GB) == "0.001GB"
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
    assert calcd == 14000.0 * MB
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
    assert functions.to_kubernetes_label(result) == "32G"
    assert functions.to_kubernetes_label(1 * KB) == "1k"
    assert functions.to_kubernetes_label(1 * kib) == "1Ki"


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
                    TemplateInputs(dryrun=True, done=DoneDict(outputs=tfvars))
                )

            cluster: unfurl_nodes_K8sCluster

        test = ExampleTerraformManagedResource(cluster=cluster)
        assert cluster_connection._default_for is cluster

    topology, runner = create_runner(Topology)
    runner.job = None
    job = runner.run(JobOptions(skip_save=True, check=True, dryrun=True))
    assert job
    assert len(job.workDone) == 2, len(job.workDone)
    task = list(job.workDone.values())[1]
    assert runner.manifest.rootResource.template.relationship_templates
    assert (
        task.outputs
        and task.outputs["cert"] == "cert.crt"
        and task.outputs["api_server"] == "https://127.0.0.1"
    )


@tosca.jinja_template(convert_to="yaml")
def email_template(self, T: tosca.TagWriter) -> str:
    return f"{self._name}@example.com"


class Topology(tosca.Namespace):
    class MyArtifact(tosca.artifacts.Root):
        staging: bool = True
        email: str
        volumes: List[Size] = tosca.DEFAULT

        @tosca.jinja_template
        def _contents(self, tag: tosca.TagWriter) -> str:
            max_size = 100 * GB
            min_size = 1 * MB
            tag.add_vars(min_size=min_size)
            return f"""
                server:
                  admin: "{self.email}"
                  host:
                  {tag.if_(self.volumes)}
                      {
                tag.for_(
                    self.volumes,
                    lambda looped: f'''
                          - {tag.index} {calc_size(looped, max_size)}
                          - {tag.expr("looped + 10000")}
                          - nested: {tag.for_(self.volumes, lambda looped2: f"{looped2}, ")}
                          - again: {tag.for_(self.volumes, lambda looped: f"{tag.expr('looped')}, ")}
                              ''',
                )
            }
                  {tag.elif_(self.staging)}
                          - size: {{{{ min_size }}}}
                  {tag.else_}
                          - size: {max_size} 
                  {tag.endif}
                  """

        contents = tosca.Computed(factory=_contents)

    class Example(tosca.nodes.Root):
        artifact: "MyArtifact"

    test = Example(
        artifact=MyArtifact(
            email=tosca.Computed(factory=email_template), volumes=[10 * GB, 20 * GB]
        )
    )


def test_artifact():
    topology, runner = create_runner(Topology)
    service_template = runner.manifest.manifest.expanded["spec"]["service_template"]
    # pprint.pprint(service_template, indent=2)
    pprint.pprint(topology.test.artifact.contents, indent=2)
    assert topology.test.artifact.contents == (
        "server:\n"
        '                  admin: "artifact@example.com"\n'
        "                  host:\n"
        "                  \n"
        "                      \n"
        "                          - 0 100GB\n"
        "                          - 10.00001GB\n"
        "                          - nested: 10GB, 20GB, \n"
        "                          - again: 10GB, 20GB, \n"
        "                              \n"
        "                          - 1 100GB\n"
        "                          - 20.00001GB\n"
        "                          - nested: 10GB, 20GB, \n"
        "                          - again: 10GB, 20GB, \n"
        "                              \n"
        "                  "
    )
    service_template.pop("repositories")
    assert service_template == {
        "tosca_definitions_version": "tosca_simple_unfurl_1_0_0",
        "topology_template": {
            "node_templates": {
                "Topology.test": {
                    "type": "Example",
                    "artifacts": {
                        "artifact": {
                            "type": "MyArtifact",
                            "properties": {
                                "email": {
                                    "eval": {
                                        "computed": [
                                            "tests.test_dsl_integration:email_template:method"
                                        ]
                                    }
                                },
                                "volumes": ["10GB", "20GB"],
                            },
                            "file": "",
                            "contents": {
                                "eval": {
                                    "computed": [
                                        "tests.test_dsl_integration:Topology.MyArtifact._contents:method"
                                    ]
                                },
                            },
                        }
                    },
                    "metadata": {"module": "tests.test_dsl_integration"},
                }
            }
        },
        "artifact_types": {
            "MyArtifact": {
                "derived_from": "tosca.artifacts.Root",
                "properties": {
                    "staging": {"type": "boolean", "default": True},
                    "email": {"type": "string"},
                    "volumes": {
                        "type": "list",
                        "entry_schema": {"type": "scalar-unit.size"},
                        "default": [],
                    },
                },
            }
        },
        "node_types": {
            "Example": {
                "derived_from": "tosca.nodes.Root",
                "artifacts": {"artifact": {"type": "MyArtifact"}},
            }
        },
    }

def test_convert():
    path = os.path.dirname(__file__) + "/fixtures/autogenerate."
    os.system(f"touch -d 2025-04-26T15:47:36 {path}yaml")
    with open(path + "yaml") as f:
        assert (
            is_python_file_newer(f.read(), path + "yaml", None, os.path.dirname(path))
            == path + "py"
        )



@pytest.mark.parametrize("variant", ["pycache", "stale_yaml", "stale_cache"])
def test_imported_module_relative_imports(tmp_path, monkeypatch, variant):
    # b.py imports c.py; b.py is converted and cached in __pycache__
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    monkeypatch.setattr(sys, "pycache_prefix", None)
    pkg_name = f"relimportpkg_{variant}"
    pkg_dir = tmp_path / pkg_name
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    (pkg_dir / "c.py").write_text(
        "import tosca\n\nclass CType(tosca.nodes.Root):\n    c_prop: str = 'c'\n"
    )
    (pkg_dir / "b.py").write_text(
        "from .c import CType\n\nclass BType(CType):\n    b_prop: str = 'b'\n"
    )
    if variant != "pycache":
        # an out-of-date conversion that lacks CType
        if variant == "stale_yaml":
            stale_yaml = pkg_dir / "c.yaml"
        else:
            (pkg_dir / "__pycache__").mkdir()
            stale_yaml = pkg_dir / "__pycache__" / "c.yaml"
        stale_yaml.write_text(
            "# Generated by tosca.python2yaml from c.py at 2020-01-01T00:00:00 overwrite not modified\n"
            "tosca_definitions_version: tosca_simple_unfurl_1_0_0\n"
        )
        generated = datetime.datetime.fromisoformat("2020-01-01T00:00:00").timestamp()
        os.utime(stale_yaml, (generated, generated))
    monkeypatch.syspath_prepend(str(tmp_path))
    BType = importlib.import_module(f"{pkg_name}.b").BType

    tosca.global_state.mode = "parse"

    class test(tosca.Namespace):
        node = BType()

    try:
        # the second run loads the yaml cached by the first
        for _ in range(2):
            manifest, doc = namespace2manifest(test)
            assert (pkg_dir / "__pycache__" / "b.yaml").is_file()
            assert manifest.tosca and manifest.tosca.template
            topology_template = manifest.tosca.template.topology_template
            assert topology_template
            custom_defs = topology_template.custom_defs
            assert "CType" in custom_defs
            assert "BType" in custom_defs
    finally:
        for name in (pkg_name, f"{pkg_name}.b", f"{pkg_name}.c"):
            sys.modules.pop(name, None)


@pytest.mark.parametrize("variant", ["edited", "regenerated", "handwritten"])
def test_edited_generated_yaml_warning(tmp_path, monkeypatch, caplog, variant):
    # c.yaml is imported by the yaml converted from b.py, shadowing c.py if it isn't regenerated
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    monkeypatch.setattr(sys, "pycache_prefix", None)
    caplog.set_level(logging.WARNING, logger="unfurl")
    pkg_name = f"editedyamlpkg_{variant}"
    pkg_dir = tmp_path / pkg_name
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    (pkg_dir / "c.py").write_text(
        "import tosca\n\nclass CType(tosca.nodes.Root):\n    c_prop: str = 'c'\n"
    )
    (pkg_dir / "b.py").write_text(
        "from .c import CType\n\nclass BType(CType):\n    b_prop: str = 'b'\n"
    )
    header = (
        ""
        if variant == "handwritten"
        else "# Generated by tosca.python2yaml from c.py at 2020-01-01T00:00:00 overwrite not modified\n"
    )
    c_yaml = pkg_dir / "c.yaml"
    c_yaml.write_text(
        header
        + """tosca_definitions_version: tosca_simple_unfurl_1_0_0
node_types:
  CType:
    derived_from: tosca.nodes.Root
    properties:
      c_yaml_prop:
        type: string
        default: yaml
"""
    )
    generated = datetime.datetime.fromisoformat("2020-01-01T00:00:00").timestamp()
    if variant != "regenerated":
        generated += 86400  # edited a day later, but still older than c.py
    os.utime(c_yaml, (generated, generated))
    monkeypatch.syspath_prepend(str(tmp_path))
    BType = importlib.import_module(f"{pkg_name}.b").BType

    tosca.global_state.mode = "parse"

    class test(tosca.Namespace):
        node = BType()

    try:
        for _ in range(2):
            manifest, doc = namespace2manifest(test)
            assert manifest.tosca and manifest.tosca.template
            topology_template = manifest.tosca.template.topology_template
            assert topology_template
            c_props = topology_template.custom_defs["CType"]["properties"]
            if variant == "regenerated":
                assert "c_prop" in c_props
            else:
                assert "c_yaml_prop" in c_props
        warnings = [
            r for r in caplog.records if "which was edited after it was generated" in r.getMessage()
        ]
        if variant == "edited":
            assert len(warnings) == 1
            message = warnings[0].getMessage()
            assert str(c_yaml) in message and str(pkg_dir / "c.py") in message
        else:
            assert not warnings
    finally:
        for name in (pkg_name, f"{pkg_name}.b", f"{pkg_name}.c"):
            sys.modules.pop(name, None)



def raise_type_error() -> str:
    raise TypeError("computed type error")


def raise_key_error() -> str:
    raise KeyError("computed key error")


computed_error_manifest = """
apiVersion: unfurl/v1.0.0
kind: Ensemble
spec:
  service_template:
    node_types:
      Source:
        derived_from: tosca.nodes.Root
        properties:
          prop:
            type: string
            required: false
    topology_template:
      node_templates:
        source:
          type: Source
          properties:
            prop:
              eval:
                computed: tests.test_dsl_integration:%s
        consumer:
          type: tosca.nodes.Root
          interfaces:
            Standard:
              operations:
                configure:
                  implementation: Template
                  inputs:
                    run:
                      eval: %s
                    done:
                      outputs:
                        out:
                          eval: $inputs::run
"""


@pytest.mark.parametrize(
    "func,message",
    [
        ("raise_type_error", "computed type error"),
        ("raise_key_error", "computed key error"),
    ],
)
def test_computed_error_fails_render(
    func: str, message: str, caplog: pytest.LogCaptureFixture
) -> None:
    manifest = YamlManifest(computed_error_manifest % (func, "::source::prop"))
    job = Runner(manifest).run(JobOptions(skip_save=True))
    assert job and job.status == Status.error
    render_errors = [
        r.exc_info[1]
        for r in caplog.records
        if "Configurator render failed" in r.getMessage() and r.exc_info
    ]
    assert len(render_errors) == 1
    assert str(render_errors[0]).startswith("error evaluating computed: ")
    assert message in str(render_errors[0])


def test_missing_key_evaluates_to_none() -> None:
    manifest = YamlManifest(
        computed_error_manifest % ("raise_type_error", "::source::no_such_prop")
    )
    job = Runner(manifest).run(JobOptions(skip_save=True))
    assert job
    (task,) = job.workDone.values()
    assert task.target.name == "consumer"
    assert task.result.success
    assert task.outputs == {"out": None}


not_found_manifest = """
apiVersion: unfurl/v1.0.0
kind: Ensemble
spec:
  service_template:
    node_types:
      Box:
        derived_from: tosca.nodes.Root
        capabilities:
          host:
            type: tosca.capabilities.Compute
      Probe:
        derived_from: tosca.nodes.Root
        requirements:
          - host:
              node: Box
              relationship: tosca.relationships.HostedOn
              occurrences: [0, 1]
        properties:
          port:
            type: integer
            default: 80
          m:
            type: map
            default: {a: 1}
          tags:
            type: list
            default: [web]
          opt:
            type: string
            required: false
          opt_size:
            type: scalar-unit.size
            required: false
          opt_list:
            type: list
            required: false
          value:
            type: any
            required: false
    topology_template:
      node_templates:
        box:
          type: Box
        other:
          type: Probe
        probe:
          type: Probe
          requirements:
            - host: box
          properties:
            value: %(expr)s
          interfaces:
            Standard:
              operations:
                configure:
                  implementation: Template
                  inputs:
                    run: %(expr)s
                    done:
                      outputs:
                        out: {eval: $inputs::run}
"""

get_property_not_found = [
    "{eval: {get_property: [SELF, undefined_prop]}}",
    "{eval: {get_property: [no_such_node, port]}}",
    "{eval: {get_property: [SELF, m, no_such_key]}}",
    "{eval: {get_property: [SELF, tags, 5]}}",
    "{eval: {get_property: [HOST, port]}}",
]


def _resolve_probe_value(expr: str) -> list:
    manifest = YamlManifest(not_found_manifest % dict(expr=expr))
    Runner(manifest).static_plan()
    root = manifest.rootResource
    assert root and root.imports is not None
    other = root.find_resource("other")
    root.imports.add_import("secret", other)
    root.imports.add_import("local", other)
    return Ref("::probe::value").resolve(RefContext(root))


@pytest.mark.parametrize(
    "expr",
    get_property_not_found
    + [
        "{eval: {get_attribute: [SELF, m, no_such_key]}}",
        "{eval: {get_attribute: [no_such_node, m]}}",
        "{eval: {get_property: [other, opt]}}",
        "{eval: {get_env: NO_SUCH_ENV_VAR}}",
        "{eval: {secret: no_such_secret}}",
        "{eval: {local: no_such_local}}",
        "{eval: {get_artifact: [SELF, no_such_artifact]}}",
        "{eval: {to_label: {eval: '.::no_such'}}}",
        "{eval: {and: ['.::no_such', 1]}}",
        "{eval: {token: ['host', ':', 1]}}",
        "{eval: {scalar: {eval: '::other::opt_size'}}}",
        "{eval: {scalar_value: {eval: '::other::opt_size'}, unit: GB}}",
    ],
)
def test_not_found_evaluates_to_none(expr: str) -> None:
    assert _resolve_probe_value(expr) == [None]


def test_foreach_over_missing_is_empty() -> None:
    assert _resolve_probe_value(
        "{eval: {foreach: [{eval: '::other::opt_list'}, '$item']}}"
    ) == _resolve_probe_value("{eval: {foreach: [[], '$item']}}")


@pytest.mark.parametrize("expr", get_property_not_found)
def test_get_property_not_found_operation_input(expr: str) -> None:
    manifest = YamlManifest(not_found_manifest % dict(expr=expr))
    job = Runner(manifest).run(JobOptions(skip_save=True))
    assert job and job.status == Status.ok
    (task,) = [t for t in job.workDone.values() if t.target.name == "probe"]
    assert task.outputs == {"out": None}


def test_operator_on_missing_value_fails() -> None:
    with pytest.raises(UnfurlEvalError, match="error evaluating gt"):
        _resolve_probe_value("{eval: {gt: [{eval: '::other::opt'}, 5]}}")
