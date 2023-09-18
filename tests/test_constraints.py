import unittest
from mypy import api
from unfurl.localenv import LocalEnv
from tosca import python2yaml
import os

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
                "properties": {"image": "myimage:latest", "url": "http://localhost:8000"},
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
                {"container": {"node": "ContainerService"}},
                {
                    "proxy": {
                        "node": "Proxy",
                        "node_filter": {
                            "properties": [
                                {
                                    "backend_url": {
                                        "eval": "$SOURCE::.targets::container::url",
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
