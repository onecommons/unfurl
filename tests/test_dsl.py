import inspect
from typing import Optional
from unittest.mock import MagicMock, patch
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


def _to_yaml(python_src: str, safe_mode) -> dict:
    namespace: dict = {}
    tosca_tpl = python_to_yaml(python_src, namespace, safe_mode=safe_mode)
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


def _generate_builtin(generate, builtin_name=None):
    import_resolver = ImportResolver(None)  # type: ignore
    python_src = generate(import_resolver, True)
    if builtin_name:
        with open(builtin_name +".py", "w") as po:
            print(python_src, file=po)
    namespace: dict = {}
    exec(python_src, namespace)
    yo = None
    if builtin_name:
        yo = open(builtin_name + ".yaml", "w")
    yaml_src = dump_yaml(namespace, yo)  # type: ignore
    if yo:
        yo.close()
    return yaml_src


def test_builtin_generation():
    yaml_src = _generate_builtin(yaml2python.generate_builtins)
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
        diffs.pop(
            "unfurl.interfaces.Install", None
        )
        if diffs:
            # these diffs exist because requirements include inherited types
            assert section == "node_types" and len(diffs) == 5

def test_builtin_ext_generation():
    assert _generate_builtin(yaml2python.generate_builtin_extensions)

default_operations_types_yaml = """
node_types:
  unfurl.nodes.Installer.Terraform:
    derived_from: unfurl.nodes.Installer
    properties:
      main:
        type: string
        required: false
        metadata:
          user_settable: false
"""
default_operations_yaml = (
    default_operations_types_yaml
    + """
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
)

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
    assert "def default(self):" in src
    tosca_tpl = _to_yaml(src, False)
    assert src_tpl["node_types"] == tosca_tpl["node_types"]

    tosca_tpl2 = _to_yaml(default_operations_python, False)
    assert src_tpl["node_types"] == tosca_tpl2["node_types"]

    # in safe_mode python parses ok but operations aren't executed and the yaml is missing interfaces
    tosca_tpl3 = _to_yaml(default_operations_python, True)
    assert (
        load_yaml(yaml, default_operations_types_yaml)["node_types"]
        == tosca_tpl3["node_types"]
    )


# from 9.3.4.2 of the TOSCA 1.3 spec
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
from tosca import *
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
    tosca_tpl = _to_yaml(src, True)
    assert src_tpl["node_types"] == tosca_tpl["node_types"]

    tosca_tpl2 = _to_yaml(example_wordpress_python, True)
    assert src_tpl["node_types"] == tosca_tpl2["node_types"]


# section 2.1 "Hello world"
example_helloworld_yaml = """
description: Template for deploying a single server with predefined properties.
topology_template:
  node_templates:
    db_server:
      type: tosca.nodes.Compute
      capabilities:
        # Host container properties
        host:
         properties:
           num_cpus: 1
           disk_size: 10 GB
           mem_size: 4096 MB
        # Guest Operating System properties
        os:
          properties:
            # host Operating System image properties
            architecture: x86_64
            type: linux
            distribution: rhel
            version: "6.5"
"""

example_helloworld_python = '''
"""Template for deploying a single server with predefined properties."""
import tosca
from tosca import *  # imports GB, MB scalars
db_server = tosca.nodes.Compute(
    "db_server",
    host=tosca.capabilities.Compute(
        num_cpus=1,
        disk_size=10 * GB,
        mem_size=4096 * MB,
    ),
    os=tosca.capabilities.OperatingSystem(
        architecture="x86_64",
        type="linux",
        distribution="rhel",
        version="6.5",
    ),
)
'''

def test_example_helloworld():
    src, src_tpl = _to_python(example_helloworld_yaml)
    tosca_tpl = _to_yaml(src, True)
    assert src_tpl == tosca_tpl
    tosca_tpl2 = _to_yaml(example_helloworld_python, True)
    assert src_tpl == tosca_tpl2

# section 2.5 example 5, page 20
example_operation_on_template_yaml = """
topology_template:
  inputs:
    wordpress_db_name:
      type: string
    wordpress_db_user:
      type: string
    wordpress_db_password:
      type: string
  node_templates:
    wordpress_db:
      type: tosca.nodes.Database
      properties:
        name: { get_input: wordpress_db_name }
        user: { get_input: wordpress_db_user }
        password: { get_input: wordpress_db_password }
      artifacts:
        db_content:
          file: files/wordpress_db_content.txt
          type: tosca.artifacts.File
      # requirements:
      #   - host: mysql
      interfaces:
        Standard:
          create:
            implementation: db_create.sh
            inputs:
              db_data: { get_artifact: [ SELF, db_content ] }
"""
def test_example_operation():
    src, src_tpl = _to_python(example_operation_on_template_yaml)
    # XXX
    # print(src)
    # tosca_tpl = _to_yaml(src, True)
    # print(tosca_tpl)
    # assert src_tpl == tosca_tpl
    # tosca_tpl2 = _to_yaml(example_operation_on_template_python, True)
    # assert src_tpl == tosca_tpl2

def test_set_constraints() -> None:
    class Example(tosca.nodes.Root):
        shellScript: tosca.artifacts.Root = tosca.artifacts.Root(file="example.sh")
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
            "artifacts": {"shellScript": {'file': 'example.sh', "type": "tosca.artifacts.Root"}},
            "requirements": [
                {
                    "host": {
                        "node": "my_compute",
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

@pytest.mark.parametrize(
    "test_input,exp_import,exp_path",
    [
        (dict(file="foo.yaml"), "from .foo import *", "/path/to/foo"),
        (dict(file="../foo.yaml"), "from ..foo import *", "/path/to/../foo"),
        (
            dict(file="../../foo.yaml"),
            "from ...foo import *",
            "/path/to/../../foo",
        ),
    ],
)
# patch repo lookup so we don't need to write the whole template
def test_convert_import(test_input, exp_import, exp_path):
    c = tosca.yaml2python.Convert(MagicMock(path="/path/to/including_file.yaml"))

    output = c.convert_import(test_input)

    # generated import
    assert output[0].strip() == exp_import
    # import path
    assert output[1] == exp_path


@pytest.mark.parametrize(
    "test_input,exp_import,exp_path",
    [
        (
            dict(repository="repo", file="foo.yaml"),
            "from tosca_repositories.repo.foo import *",
            "tosca_repositories/repo/foo",
        ),
        (
            dict(repository="repo", file="subdir/foo.yaml"),
            "from tosca_repositories.repo.subdir.foo import *",
            "tosca_repositories/repo/subdir/foo",
        ),
        (
            dict(repository="repo", file="foo.yaml", namespace_prefix="tosca.ns"),
            "from tosca_repositories.repo import foo as tosca.ns",
            "tosca_repositories/repo/foo",
        ),
        (
            dict(
                repository="repo", file="subdir/foo.yaml", namespace_prefix="tosca.ns"
            ),
            "from tosca_repositories.repo.subdir import foo as tosca.ns",
            "tosca_repositories/repo/subdir/foo",
        ),
        (
            dict(repository="repo", file="foo.yaml", namespace_prefix="ns"),
            "from tosca_repositories.repo import foo as ns",
            "tosca_repositories/repo/foo",
        ),
    ],
)
# patch repo lookup so we don't need to write the whole template
def test_convert_import_with_repo(test_input, exp_import, exp_path):
    with patch.object(
        tosca.yaml2python.Convert,
        "find_repository",
        return_value=(
            f"tosca_repositories.{test_input.get('repository')}",
            f"tosca_repositories/{test_input.get('repository')}",
        ),
    ):
        c = tosca.yaml2python.Convert(MagicMock())

        output = c.convert_import(test_input)

        # generated import
        assert output[0].strip() == exp_import
        # import path
        assert output[1] == exp_path


def test_sandbox():
    # disallowed imports parse but raise ImportError when accessed
    imports = [
        "import sys",
        "import tosca.python2yaml",
        "from tosca.python2yaml import ALLOWED_MODULE, missing",
    ]
    for src in imports:
        assert _to_yaml(src, True)

    imports = [
        "import sys; sys.version_info",
        "from tosca import python2yaml",
        "import tosca_repositories",
        """from tosca.python2yaml import ALLOWED_MODULE, missing
str(ALLOWED_MODULE)
    """,
    ]
    for src in imports:
        with pytest.raises(ImportError):
            assert _to_yaml(src, True)

    denied = [
        """import tosca
tosca.python2yaml""",
        """import tosca
tosca.global_state""",
        """import tosca
tosca.pown = 1""",
        """import tosca
tosca.Namepace.location = 'pown'""",
        "import tosca.python2yaml; tosca.python2yaml.ALLOWED_MODULE",
    ]
    # deny unsafe builtins
    for src in denied:
        with pytest.raises(AttributeError):
            assert _to_yaml(src, True)

    denied = [
        """import tosca
getattr(tosca, 'global_state')""",
        """import tosca
setattr(tosca, "pown", 1)""",
        """import math
math.__loader__.create_module = 'pown'""",
        """import tosca
tosca.nodes.Root = 1""",
        """import tosca
tosca.nodes.Root._tosca_name = 'pown'""",
    ]
    for src in denied:
        # misc errors: SyntaxError, NameError, TypeError
        with pytest.raises(Exception):
            assert _to_yaml(src, True)

    allowed = [
        """foo = {}; foo[1] = 2; bar = []; bar.append(1); baz = ()""",
        """foo = dict(); foo[1] = 2; bar = list(); bar.append(1); baz = tuple()""",
        """import math; math.floor(1.0)""",
        """import tosca
node = tosca.nodes.Root()
node._name = "test"
        """,
    ]
    for src in allowed:
        assert _to_yaml(src, True)


if __name__ == "__main__":
    _generate_builtin(yaml2python.generate_builtins, "builtin_types")
    _generate_builtin(yaml2python.generate_builtin_extensions, "tosca_ext")
