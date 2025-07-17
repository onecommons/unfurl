import pprint
from typing import List, Optional
import unittest

import pytest

import unfurl
from unfurl.job import Runner
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
from unfurl.util import UnfurlError, change_cwd
from unfurl.testing import assert_no_mypy_errors


def test_constraints(caplog):
    basepath = os.path.join(os.path.dirname(__file__), "examples/")
    # loads yaml with with a json include
    local = LocalEnv(basepath + "constraints-ensemble.yaml")
    manifest = local.get_manifest(skip_validation=False, safe_mode=True)
    service_template = manifest.manifest.expanded["spec"]["service_template"]
    node_templates = {
        "myapp": {
            "type": "App",
            "metadata": {"module": "service_template.constraints"},
            "requirements": [
                {"proxy": "myapp_proxy"},
                {"container":
                 {"node" : "container_service"}},
            ],
        },
        "container_service": {
            "type": "ContainerService",
            "properties": {
                "image": "myimage:latest",
                "url": "http://localhost:8000",
                "name": "app",  # applied by the app's node_filter
                "mem_size": "1 GB",  # XXX node_filter constraints aren't being applied
            },
            'metadata': {'module': 'service_template.constraints'}
        },
        "myapp_proxy": {
            "type": "ProxyContainerHost",
            'directives': ['dependent'],
             'metadata': {'module': 'service_template.constraints'}
        },
    }
    for name, value in node_templates.items():
        # pprint.pprint((name, service_template["topology_template"]["node_templates"][name]), indent=4)
        assert (
            service_template["topology_template"]["node_templates"][name] == value
        ), name

    myapp_proxy_spec = manifest.tosca.topology.get_node_template("myapp_proxy")
    assert myapp_proxy_spec
    expected_prop_value = {
        "eval": "$SOURCE::.targets::container::url",
        "vars": {"SOURCE": {"eval": "::myapp"}},
    }
    assert myapp_proxy_spec.properties["backend_url"] == expected_prop_value
    assert (
        myapp_proxy_spec.toscaEntityTemplate.get_property_value("backend_url")
        == expected_prop_value
    )
    node_types = {
        "ContainerService": {
            "derived_from": "tosca.nodes.Root",
            "properties": {
                "image": {"type": "string"},
                "url": {"type": "string"},
                "mem_size": {"type": "scalar-unit.size"},
                "name": {"type": "string"},
            },
        },
        "ContainerHost": {
            "derived_from": "tosca.nodes.Root",
            "requirements": [
                {
                    "hosting": {
                        "node": "ContainerService",
                        "!namespace-node": "github.com/onecommons/unfurl.git/tests/examples:constraints-ensemble",
                    }
                }
            ],
        },
        "Proxy": {
            "derived_from": "tosca.nodes.Root",
            "properties": {
                "backend_url": {
                    "type": "string",
                    "description": "URL to proxy",
                    "!namespace": "github.com/onecommons/unfurl.git/tests/examples:constraints-ensemble",
                }
            },
            "attributes": {
                "endpoint": {
                    "type": "string",
                    "!namespace": "github.com/onecommons/unfurl.git/tests/examples:constraints-ensemble",
                    "description": "Public URL",
                }
            },
        },
        "ProxyContainerHost": {
            "derived_from": ["Proxy", "ContainerHost"],
            "requirements": [
                {
                    "hosting": {
                        "node_filter": {"match": [{"eval": "backend_url"}]},
                    }
                }
            ],
        },
        "App": {
            "derived_from": "tosca.nodes.Root",
            "requirements": [
                {
                    "container": {
                        "node": "container_service",
                        "!namespace-node": "github.com/onecommons/unfurl.git/tests/examples:constraints-ensemble",
                    }
                },
                {
                    "proxy": {
                        "node": "ProxyContainerHost",
                        "!namespace-node": "github.com/onecommons/unfurl.git/tests/examples:constraints-ensemble",
                        "node_filter": {
                            "properties": [
                                {
                                    "backend_url": {
                                        "eval": "$SOURCE::.targets::container::url",
                                    }
                                }
                            ],
                            "requirements": [
                                {
                                    "hosting": {
                                        "node_filter": {
                                            "properties": [
                                                {"name": {"q": "app"}},
                                                {
                                                    "mem_size": {
                                                        "in_range": ["2 GB", "20 GB"]
                                                    }
                                                },
                                            ]
                                        }
                                    }
                                }
                            ],
                        },
                    }
                },
            ],
        },
    }
    for name, value in node_types.items():
        # pprint.pprint((name, service_template["node_types"][name]), indent=4)
        assert service_template["node_types"][name] == value, name

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

    Runner(manifest).static_plan()  # generate instances
    with pytest.raises(UnfurlError, match='The value "1 GB" of property "mem_size" is out of range'):
        assert manifest.get_root_resource().find_instance("container_service").attributes["mem_size"] == "1 GB"

    assert "Solver set myapp_proxy.hosting to container" in caplog.text
    # XXX support for deducing inverse and test
    # assert container.get_relationship("host") == proxy



@unittest.skipIf("slow" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
@pytest.mark.parametrize(
    "path", ["constraints.py", "dsl_configurator.py", "dsl_relationships.py", "dsl_artifacts.py"]
)
def test_mypy(path):
    # assert mypy ok
    basepath = os.path.join(os.path.dirname(__file__), "examples", path)
    assert_no_mypy_errors(basepath) #, "--disable-error-code=override")


@unittest.skipIf("slow" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
def test_mypy_errors():
    # assert mypy expected errors
    expected = [
        'error: Unsupported operand types for * ("Size" and "str")  [operator]',
        'Unsupported operand types for * ("Size" and "_Unit[Frequency]")  [operator]',
        'Argument 1 to "apply_constraint" of "DataConstraint" has incompatible type "str"; expected ',
        'Unsupported operand types for + ("Size" and "Frequency")',
        'Argument 1 to "as_int" of "_Unit" has incompatible type "Size"; expected "Frequency"',
        "Found 6 errors in 1 file",
    ]
    basepath = os.path.join(os.path.dirname(__file__), "examples", "type_errors.py")
    assert_no_mypy_errors(basepath, expected=expected)


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
            min_length(2).apply_constraint(cls.name)
            # you can also but you lose static type checking:
            cls.name = max_length(20)  # type: ignore
            # setting a constraint on reference to requirement creates a node_filter:
            in_range(2 * gb, 20 * gb).apply_constraint(cls.host.host.mem_size)

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
    +include: mytypes.py
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
        data_list:
          value:
            eval: ::test::data_list
        extra:
          value:
            eval: ::generic::copy_of_extra
changes: []
"""


with open(os.path.join(os.path.dirname(__file__), "examples/dsl_configurator.py")) as f:
    attribute_access_import = f.read()


def test_computed_properties():
    cli_runner = CliRunner()
    with cli_runner.isolated_filesystem() as tmp:
        init_project(
            cli_runner,
            env=dict(UNFURL_HOME=""),
        )
        with open("ensemble-template.yaml", "w") as f:
            f.write(attribute_access_ensemble)

        with open("mytypes.py", "w") as f:
            f.write(attribute_access_import)

        result, job, summary = run_job_cmd(cli_runner, print_result=True)
        expected = {
            "computed": "https://foo.com",
            "url": "https://foo.com",
            "ports": {
                "target": 8080,
                "source": 80,
            },
            "a_list": [1],
            "data_list": [
                {
                    "ports": {
                        "target": 8080,
                        "source": 80,
                    },
                    "additional": 1,
                }
            ],
            "extra": "extra",
        }
        # pprint.pprint(job.get_outputs())
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
        result, job, summary = run_job_cmd(
            cli_runner, ["-vvv", "undeploy"], print_result=True
        )
        assert job.get_outputs()["computed"] == "set output"
        assert summary["job"] == {
            "id": "A01110GC0000",
            "status": "ok",
            "total": 1,
            "ok": 1,
            "error": 0,
            "unknown": 0,
            "skipped": 0,
            "changed": 1,
        }
        # print( result.output )
        # with open("ensemble/ensemble.yaml") as f:
        #     print(f.read())


relationships_yaml = {
    "topology_template": {},
    "tosca_definitions_version": "tosca_simple_unfurl_1_0_0",
    "node_types": {
        "Volume": {
            "derived_from": "tosca.nodes.Root",
            "properties": {
                "disk_label": {"type": "string"},
                "disk_size": {"type": "scalar-unit.size", "default": "100 GB"},
            },
        },
        "TestTarget": {
            "derived_from": "tosca.nodes.Root",
            "artifacts": {
                "volume_mount": {
                    "type": "VolumeMountArtifact",
                    "properties": {
                        "mountpoint": "/mnt/{{ {'eval': {'computed': ['service_template.dsl_relationships:disk_label', {'eval': '.targets::volume_attachment::.target::disk_label'}]}} | map_value }}",
                    },
                    "file": "",
                    "intent": "mount",
                    "target": "HOST",
                }
            },
            "requirements": [
                {
                    "volume_attachment": {
                        "relationship": "VolumeAttachment",
                        "node": "Volume",
                        "occurrences": [0, 1],
                    }
                }
            ],
        },
    },
    "relationship_types": {
        "VolumeAttachment": {
            "derived_from": "tosca.relationships.AttachesTo",
        }
    },
    "artifact_types": {
        "VolumeMountArtifact": {
            "derived_from": "tosca.artifacts.Root",
            "properties": {"mountpoint": {"type": "string"}},
        }
    },
}


def test_relationships():
    basepath = os.path.join(os.path.dirname(__file__), "examples/")
    # loads yaml with with a json include
    local = LocalEnv(basepath + "dsl-ensemble.yaml")  # loads dsl_relationships.py
    manifest = local.get_manifest(skip_validation=True, safe_mode=True)
    service_template = manifest.manifest.expanded["spec"]["service_template"]
    # pprint.pprint(service_template, indent=2)
    service_template.pop("repositories")
    assert service_template == relationships_yaml
