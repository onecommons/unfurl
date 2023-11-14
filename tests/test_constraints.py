from typing import List, Optional
import unittest

import unfurl
from .utils import init_project, run_job_cmd
from mypy import api
import tosca
from unfurl.localenv import LocalEnv
from tosca import python2yaml
import os
import sys
from unfurl.yamlloader import yaml, load_yaml
from tosca.python2yaml import PythonToYaml
from click.testing import CliRunner


def _verify_mypy(path):
    stdout, stderr, return_code = api.run([path])
    assert "no issues found in 1 source file" in stdout
    assert return_code == 0, stderr


def test_constraints():
    basepath = os.path.join(os.path.dirname(__file__), "examples/")
    # loads yaml with with a json include
    local = LocalEnv(basepath + "constraints-ensemble.yaml")
    manifest = local.get_manifest(skip_validation=True, safe_mode=True)
    service_template = manifest.manifest.expanded["spec"]["service_template"]
    assert service_template["topology_template"] == {
        "node_templates": {
            "myapp": {
                "type": "App",
                "metadata": {"module": "service_template.constraints"},
                "requirements": [
                    {"container": "container_service"},
                    {"proxy": "myapp_proxy"},
                ],
            },
            "container_service": {
                "type": "ContainerService",
                "properties": {
                    "image": "myimage:latest",
                    "url": "http://localhost:8000",
                },
            },
            "myapp_proxy": {
                "type": "ProxyContainerHost",
                "properties": {
                    "backend_url": {
                        "eval": "$SOURCE::.targets::container::url",
                        "vars": {"SOURCE": {"eval": "::myapp"}},
                    }
                },
            },
        }
    }
    assert service_template["node_types"] == {
        "ContainerService": {
            "derived_from": "tosca.nodes.Root",
            "properties": {"image": {"type": "string"}, "url": {"type": "string"}},
        },
        "ContainerHost": {
            "derived_from": "tosca.nodes.Root",
            "requirements": [{"hosting": {"node": "ContainerService"}}],
        },
        "Proxy": {
            "derived_from": "tosca.nodes.Root",
            "properties": {
                "backend_url": {
                    "type": "string",
                    "default": None,
                    "description": "URL to proxy",
                }
            },
            "attributes": {"endpoint": {"type": "string", "description": "Public URL"}},
        },
        "ProxyContainerHost": {
            "derived_from": ["Proxy", "ContainerHost"],
            "requirements": [
                {
                    "hosting": {
                        "node": "ContainerService",
                        "node_filter": {"match": [{"eval": "backend_url"}]},
                    }
                }
            ],
        },
        "App": {
            "derived_from": "tosca.nodes.Root",
            "requirements": [
                {"container": {"node": "container_service"}},
                {
                    "proxy": {
                        "node": "Proxy",
                        "node_filter": {
                            "properties": [
                                {
                                    "backend_url": {
                                        "eval": "$SOURCE::.targets::container::url",
                                        # vars should be omitted, set at parse time per template
                                        "vars": {"SOURCE": {"eval": "::myapp"}},
                                    }
                                }
                            ]
                        },
                    }
                },
            ],
        },
    }

    root = manifest.tosca.topology.get_node_template("myapp")
    assert root
    # set by _constraint
    proxy = root.get_relationship("proxy").target
    assert proxy and proxy.name == "myapp_proxy"
    container = root.get_relationship("container").target
    assert container
    # deduced container from backend_url
    hosting = proxy.get_relationship("hosting").target
    assert hosting == container, (hosting, container)
    # XXX deduced inverse
    # assert container.get_relationship("host") == proxy


@unittest.skipIf("slow" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
def test_mypy():
    # assert mypy ok
    basepath = os.path.join(os.path.dirname(__file__), "examples/")
    _verify_mypy(basepath + "constraints.py")


constraints_yaml = """
tosca_definitions_version: tosca_simple_unfurl_1_0_0
node_types:
  Example:
    derived_from: tosca.nodes.Root
    properties:
      name:
        type: string
        constraints:
        - min_length: 2
        - max_length: 20
    requirements:
    - host:
        node: tosca.nodes.Compute
        node_filter:
          capabilities:
          - host:
              properties:
              - mem_size:
                  in_range:
                  - 2 GB
                  - 20 GB
topology_template: {}
"""


def test_class_init() -> None:
    from tosca import min_length, max_length, in_range, gb

    class Example(tosca.nodes.Root):
        name: str
        host: tosca.nodes.Compute

        @classmethod
        def _class_init(cls) -> None:
            #
            min_length(2).apply_constraint(cls.name)
            # you can also but you lose static type checking:
            cls.name = max_length(20)  # type: ignore
            # setting a constraint on reference to requirement creates a node_filter:
            in_range(2 * gb, 20 * gb).apply_constraint(cls.host.host.mem_size)
            # cls.container.host.host.mem_size = in_range(2*gb, 20*gb)

    __name__ = "tests.test_constraints"
    converter = PythonToYaml(locals())
    yaml_dict = converter.module2yaml()
    tosca_yaml = load_yaml(yaml, constraints_yaml)
    # yaml.dump(yaml_dict, sys.stdout)
    assert tosca_yaml == yaml_dict


attribute_access_ensemble = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
spec:
  service_template:
    +include: types.py
    topology_template:
      outputs:
        computed:
          value:
            eval: ::test::computed
        url:
          value:
            eval: ::test::url
        ports:
          value:
            eval: ::test::data::ports
        a_list:
          value:
            eval: ::test::int_list
        extra:
          value:
            eval: ::generic::copy_of_extra
"""

attribute_access_import = """
import tosca
import unfurl
from typing import List, Optional

class Test(tosca.nodes.Root):
    url_scheme: str
    host: str
    computed: str = tosca.Attribute()
    data: "MyDataType" = tosca.DEFAULT
    int_list: List[int] = tosca.DEFAULT
    data_list: List["MyDataType"] = tosca.DEFAULT
    a_requirement: unfurl.nodes.Generic

    @tosca.computed(
        title="URL",
        metadata={"sensitive": True},
    )
    def url(self) -> str:
        return f"{ self.url_scheme }://{self.host }"

    def run(self, task):
        self.computed = self.url
        self.data.ports.source = 80
        self.data.ports.target = 8080
        self.int_list.append(1)
        extra = self.a_requirement.extra
        self.a_requirement.copy_of_extra = extra

        # XXX make this work:
        # self.data_list.append(MyDataType())
        # self.data_list[0].ports.source = 80
        # self.data_list[0].ports.target = 8080
        return True

    def create(self, **kw):
        return self.run

class MyDataType(tosca.DataType):
    ports: tosca.datatypes.NetworkPortSpec = tosca.Property(
                factory=lambda: tosca.datatypes.NetworkPortSpec(**tosca.PortSpec.make(80))
              )

generic = unfurl.nodes.Generic("generic")
generic.extra = "extra"
test = Test(url_scheme="https", host="foo.com", a_requirement=generic)
"""


def test_computed_properties():
    cli_runner = CliRunner()
    with cli_runner.isolated_filesystem():
        init_project(
            cli_runner,
            env=dict(UNFURL_HOME=""),
        )
        with open("ensemble-template.yaml", "w") as f:
            f.write(attribute_access_ensemble)

        with open("types.py", "w") as f:
            f.write(attribute_access_import)

        result, job, summary = run_job_cmd(cli_runner, print_result=True)
        expected = {
            "computed": "https://foo.com",
            "url": "https://foo.com",
            "ports": {
                "protocol": None,
                "target": 8080,
                "target_range": None,
                "source": 80,
                "source_range": None,
            },
            "a_list": [1],
            "extra": "extra",
        }
        assert job.get_outputs() == expected
        assert job.json_summary()["job"] == {
            "id": "A01110000000",
            "status": "ok",
            "total": 1,
            "ok": 1,
            "error": 0,
            "unknown": 0,
            "skipped": 0,
            "changed": 1,
        }
