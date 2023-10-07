from typing import Optional
import unittest
import unfurl
from mypy import api
import tosca
from unfurl.localenv import LocalEnv
from tosca import python2yaml
import os
import sys
from unfurl.yamlloader import yaml, load_yaml
from tosca.python2yaml import PythonToYaml


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
            "properties": {"backend_url": {"type": "string", "default": None}},
            "attributes": {"endpoint": {"type": "string"}},
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


def test_set_constraints() -> None:
    from tosca import min_length, max_length, in_range, gb

    class Example(tosca.nodes.Root):
        name: str
        host: tosca.nodes.Compute

        @classmethod
        def _set_constraints(cls) -> None:
            #
            min_length(2).apply_constraint(cls.name)
            # you can also but you lose static type checking:
            cls.name = max_length(20)  # type: ignore
            # setting a constraint on reference to requirement creates a node_filter:
            in_range([2 * gb, 20 * gb]).apply_constraint(cls.host.host.mem_size)
            # cls.container.host.host.mem_size = in_range([2*gb, 20*gb])

    __name__ = "tests.test_constraints"
    converter = PythonToYaml(locals())
    yaml_dict = converter.module2yaml()
    tosca_yaml = load_yaml(yaml, constraints_yaml)
    # yaml.dump(yaml_dict, sys.stdout)
    assert tosca_yaml == yaml_dict
