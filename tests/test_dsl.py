import sys
from unfurl.merge import diff_dicts
from tosca import yaml2python, convert_to_tosca, dump_yaml
from toscaparser.elements.entity_type import EntityType
from unfurl.yamlloader import ImportResolver, load_yaml, yaml
from toscaparser.tosca_template import ToscaTemplate


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
        print(yaml2python.value2python_repr(diffs))
        if diffs:
            # these diffs exist because requirements include inherited types
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
from tosca import operation, Property, Ref

class unfurl_nodes_Installer_Terraform(unfurl.nodes.Installer):
    _tosca_name = "unfurl.nodes.Installer.Terraform"
    main: str | None = Property(metadata={"user_settable": False}, default=None)

    @operation(apply_to=["Install.check", "Standard.delete"])
    def default(self):
        return unfurl.configurators.terraform.TerraformConfigurator(
            main=Ref({"get_property": ["SELF", "main"]}),
        )
"""


def _to_python(yaml_str: str):
    tosca_yaml = load_yaml(yaml, yaml_str)
    tosca_yaml["tosca_definitions_version"] = "tosca_simple_unfurl_1_0_0"
    if "topology_template" not in tosca_yaml:
        tosca_yaml["topology_template"] = dict(
            node_templates={}, relationship_templates={}
        )
    import_resolver = ImportResolver(None)  # type: ignore
    template = ToscaTemplate(
        path=__file__, yaml_dict_tpl=tosca_yaml, import_resolver=import_resolver
    )
    src = yaml2python.convert_service_template(template)
    return src, tosca_yaml


def _to_yaml(python_src: str) -> dict:
    namespace = {}
    tosca_tpl = convert_to_tosca(python_src, namespace)
    # yaml.dump(tosca_tpl, sys.stdout)
    return tosca_tpl

def test_default_operations():
    src, src_tpl = _to_python(default_operations_yaml)
    tosca_tpl = _to_yaml(src)
    assert src_tpl["node_types"] == tosca_tpl["node_types"]


if __name__ == "__main__":
    dump = True
    test_builtin_generation()
