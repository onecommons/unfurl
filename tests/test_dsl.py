import inspect
from typing import Optional

import pytest
from unfurl.merge import diff_dicts
import sys

try:
    from tosca import yaml2python
except ImportError:
    print(sys.path)
    raise
from tosca.python2yaml import PythonToYaml, python_to_yaml, dump_yaml
from toscaparser.elements.entity_type import EntityType
from unfurl.yamlloader import ImportResolver, load_yaml, yaml
from toscaparser.tosca_template import ToscaTemplate
import tosca


def _to_python(yaml_str: str):
    tosca_yaml = load_yaml(yaml, yaml_str)
    tosca_yaml["tosca_definitions_version"] = "tosca_simple_unfurl_1_0_0"
    if "topology_template" not in tosca_yaml:
        tosca_yaml["topology_template"] = dict(
            node_templates={}, relationship_templates={}
        )
    import_resolver = ImportResolver(None)  # type: ignore
    src = yaml2python.yaml_to_python(
        __file__, tosca_dict=tosca_yaml, import_resolver=import_resolver
    )
    return src, tosca_yaml


def _to_yaml(python_src: str) -> dict:
    namespace: dict = {}
    tosca_tpl = python_to_yaml(python_src, namespace)
    # yaml.dump(tosca_tpl, sys.stdout)
    return tosca_tpl


def test_builtin_name():
    template = ToscaTemplate(
        path=EntityType.TOSCA_DEF_FILE,
        yaml_dict_tpl=EntityType.TOSCA_DEF_LOAD_AS_IS,
    )
    name = yaml2python.Convert(
        template, builtin_prefix=f"tosca.nodes.", custom_defs=EntityType.TOSCA_DEF
    )._get_name("tosca.nodes.Abstract.Compute", "typename")[0]
    assert name == "tosca.nodes.AbstractCompute", name


dump = False


def test_builtin_generation():
    import_resolver = ImportResolver(None)  # type: ignore
    python_src = yaml2python.generate_builtins(import_resolver, True)
    if dump:
        with open("builtin_ext.py", "w") as po:
            print(python_src, file=po)
    namespace: dict = {}
    exec(python_src, namespace)
    yo = None
    if dump:
        yo = open("builtin_ext.yaml", "w")
    yaml_src = dump_yaml(namespace, yo)  # type: ignore
    if yo:
        yo.close()
    src_yaml = EntityType.TOSCA_DEF_LOAD_AS_IS
    for section in EntityType.TOSCA_DEF_SECTIONS:
        print(section)
        assert len(src_yaml[section]) == len(yaml_src[section]), (
            section,
            list(src_yaml[section]),
            list(yaml_src[section]),
        )
        diffs = diff_dicts(
            src_yaml[section], yaml_src[section], skipkeys=("description", "required")
        )
        # print(yaml2python.value2python_repr(diffs))
        if diffs:
            # these diffs exist because requirements include inherited types
            diffs.pop(
                "tosca.nodes.Root", None
            )  # this one might exist depending on test execution order
            assert section == "node_types" and len(diffs) == 5


default_operations_yaml = """
node_types:
  unfurl.nodes.Installer.Terraform:
    derived_from: unfurl.nodes.Installer
    properties:
      main:
        type: string
        required: false
        metadata:
          user_settable: false
    interfaces:
      defaults:
        implementation:
          className: unfurl.configurators.terraform.TerraformConfigurator
        inputs:
          main: { get_property: [SELF, main] }
      Standard:
        operations:
          delete:
      Install:
        operations:
          check:
  """

default_operations_python = """
import unfurl
import unfurl.configurators.terraform
from tosca import operation, Property, Eval
from typing import Union

class unfurl_nodes_Installer_Terraform(unfurl.nodes.Installer):
    _tosca_name = "unfurl.nodes.Installer.Terraform"
    main: Union[str , None] = Property(metadata={"user_settable": False}, default=None)

    @operation(apply_to=["Install.check", "Standard.delete"])
    def default(self):
        return unfurl.configurators.terraform.TerraformConfigurator(
            main=Eval({"get_property": ["SELF", "main"]}),
        )
foo = 1
"""


def test_default_operations():
    src, src_tpl = _to_python(default_operations_yaml)
    tosca_tpl = _to_yaml(src)
    assert src_tpl["node_types"] == tosca_tpl["node_types"]

    tosca_tpl2 = _to_yaml(default_operations_python)
    assert src_tpl["node_types"] == tosca_tpl2["node_types"]


# from 9.3.4.2
example_wordpress_yaml = """
node_types:
  WordPress:
    derived_from: tosca.nodes.WebApplication
    description: Description of the Wordpress type
    properties:
        admin_user:
          type: string
        admin_password:
          type: string
        db_host:
          type: string
          description: Description of the db_host property
    requirements:
      - database_endpoint:
          description: Description of the database_endpoint requirement
          capability: tosca.capabilities.Endpoint.Database
          node: tosca.nodes.Database
          relationship: tosca.relationships.ConnectsTo
"""

example_wordpress_python = '''
import tosca
class WordPress(tosca.nodes.WebApplication):
    """
    Description of the Wordpress type
    """
    admin_user: str
    admin_password: str
    db_host: str
    "Description of the db_host property"

    database_endpoint: "tosca.relationships.ConnectsTo | tosca.nodes.Database | tosca.capabilities.EndpointDatabase"
    "Description of the database_endpoint requirement"
'''


def test_example_wordpress():
    src, src_tpl = _to_python(example_wordpress_yaml)
    tosca_tpl = _to_yaml(src)
    assert src_tpl["node_types"] == tosca_tpl["node_types"]

    tosca_tpl2 = _to_yaml(example_wordpress_python)
    assert src_tpl["node_types"] == tosca_tpl2["node_types"]


def test_set_constraints() -> None:
    class Example(tosca.nodes.Root):
        shellScript: tosca.artifacts.Root = tosca.artifacts.Root(
            "example.sh"
        )  # (file="example.sh")
        prop1: Optional[str] = tosca.Eval()
        host: tosca.nodes.Compute = tosca.Requirement(default=None)

        @classmethod
        def _set_constraints(cls) -> None:
            cls.host.public_address = cls.prop1 or ""

            with pytest.raises(ValueError) as e_info1:
                cls.host.host = cls.host.host
            assert '"host" is a capability, not a TOSCA property' in str(e_info1)

            # XXX need to add anonymous templates created and assigned here to yaml
            cls.host = tosca.nodes.Compute("my_compute")
            cls.prop1 = cls.host.os.distribution
            # same as cls.host = cls.prop1 but avoids the static type mismatch error
            cls.set_source(cls.host, cls.prop1)

        def create(self) -> tosca.artifacts.Root:
            return self.shellScript.execute(input1=self.prop1)

    # print( str(inspect.signature(Example.__init__)) )

    my_template = Example("my_template")

    assert my_template.prop1 == {"eval": "::my_template::prop1"}
    __name__ = "tests.test_dsl"
    converter = PythonToYaml(locals())
    yaml_dict = converter.module2yaml()
    assert yaml_dict["node_types"] == {
        "Example": {
            "derived_from": "tosca.nodes.Root",
            "properties": {
                "prop1": {
                    "type": "string",
                    "required": False,
                    "default": {
                        "eval": ".targets::host::.capabilities[.name=os]::distribution"
                    },
                }
            },
            "artifacts": {"shellScript": {"type": "tosca.artifacts.Root"}},
            "requirements": [
                {
                    "host": {
                        "node": "tosca.nodes.Compute",  # XXX should reference my_compute
                        "node_filter": {
                            "match": [{"eval": "prop1"}],
                            "properties": [
                                {"public_address": {"eval": "$SOURCE::prop1"}}
                            ],
                        },
                    }
                }
            ],
            "interfaces": {
                "Standard": {
                    "operations": {
                        "create": {
                            "implementation": {"primary": "shellScript"},
                            "inputs": {"input1": {"eval": ".::prop1"}},
                        }
                    }
                }
            },
        }
    }


if __name__ == "__main__":
    dump = True
    test_builtin_generation()
