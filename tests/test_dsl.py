import dataclasses
import inspect
import os
import time
from typing import Optional
import unittest
from unittest.mock import MagicMock, patch
import pytest
from pprint import pprint
from unfurl.merge import diff_dicts
import sys
from click.testing import CliRunner
from unfurl.__main__ import cli

try:
    from tosca import yaml2python
except ImportError:
    print(sys.path)
    raise

from tosca.python2yaml import PythonToYaml, python_src_to_yaml_obj
from toscaparser.elements.entity_type import EntityType, globals
from unfurl.yamlloader import ImportResolver, load_yaml, yaml
from unfurl.manifest import Manifest
from toscaparser.tosca_template import ToscaTemplate
import tosca
import unfurl


def _to_python(
    yaml_str: str,
    python_target_version=None,
    write_policy=tosca.WritePolicy.never,
    manifest=None,
):
    tosca_yaml = load_yaml(
        yaml, yaml_str, readonly=True
    )  # export uses readonly yaml parser
    tosca_yaml["tosca_definitions_version"] = "tosca_simple_unfurl_1_0_0"
    if "topology_template" not in tosca_yaml:
        tosca_yaml["topology_template"] = dict(
            node_templates={}, relationship_templates={}
        )
    import_resolver = ImportResolver(manifest)  # type: ignore
    import_resolver.readonly = True
    current = globals._annotate_namespaces
    try:
        globals._annotate_namespaces = False
        src = yaml2python.yaml_to_python(
            __file__,
            tosca_dict=tosca_yaml,
            import_resolver=import_resolver,
            python_target_version=python_target_version,
            write_policy=write_policy,
        )
    finally:
        globals._annotate_namespaces = current
    return src, tosca_yaml


def _to_yaml(python_src: str, safe_mode) -> dict:
    namespace: dict = {}
    current = globals._annotate_namespaces
    try:
        globals._annotate_namespaces = False
        tosca_tpl = python_src_to_yaml_obj(python_src, namespace, safe_mode=safe_mode)
    finally:
        globals._annotate_namespaces = current
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


def dump_yaml(namespace, out=sys.stdout):
    from unfurl.yamlloader import yaml

    converter = PythonToYaml(namespace)
    doc = converter.module2yaml()
    if out:
        yaml.dump(doc, out)
    return doc


def _generate_builtin(generate, builtin_path=None):
    import_resolver = ImportResolver(None)  # type: ignore
    python_src = generate(import_resolver, True)
    if builtin_path:
        path = os.path.abspath(builtin_path + ".py")
        print("*** writing source to", path)
        with open(path, "w") as po:
            print(python_src, file=po)
    namespace: dict = {}
    exec(python_src, namespace)
    yo = None
    # if builtin_name:
    #     path = os.path.abspath(builtin_name + ".yaml")
    #     yo = open(path, "w")
    yaml_src = dump_yaml(namespace, yo)  # type: ignore
    if yo:
        yo.close()
    return yaml_src


def test_builtin_generation():
    yaml_src = _generate_builtin(yaml2python.generate_builtins)
    src_yaml = EntityType.TOSCA_DEF_LOAD_AS_IS
    for section in EntityType.TOSCA_DEF_SECTIONS:
        if section == "types":
            continue
        print(section)
        assert len(src_yaml[section]) == len(yaml_src[section]), (
            section,
            list(src_yaml[section]),
            list(yaml_src[section]),
        )
        diffs = diff_dicts(
            src_yaml[section], yaml_src[section], skipkeys=("description", "required")
        )
        diffs.pop("unfurl.interfaces.Install", None)
        diffs.pop(
            "tosca.nodes.Root", None
        )  # XXX sometimes present due to test race condition?
        diffs.pop(
            "tosca.nodes.SoftwareComponent", None
        )  # !namespace attributes might get added by other tests
        print(yaml2python.value2python_repr(diffs))
        if diffs:
            # these diffs exist because requirements include inherited types
            assert section == "node_types" and len(diffs) == 5


def test_builtin_ext_generation():
    assert _generate_builtin(yaml2python.generate_builtin_extensions)


type_reference_python = '''
import tosca
from tosca import *
from typing import Sequence, Dict

class WordPress(tosca.nodes.WebApplication):
    """
    Description of the Wordpress type
    """
    admin_user: str
    admin_password: str
    db_host: str
    "Description of the db_host property"

    # test forward references in type defined later in the module
    plugins: Sequence["WordPressPlugin"] = ()

class WordPressPlugin(tosca.nodes.Root):
    name: str
    instance: str = WordPress.app_endpoint.ip_address

class MyPlugin(WordPressPlugin):
    settings: dict
'''

type_reference_yaml = {
    "tosca_definitions_version": "tosca_simple_unfurl_1_0_0",
    "node_types": {
        "WordPress": {
            "derived_from": "tosca.nodes.WebApplication",
            "description": "Description of the Wordpress type",
            "properties": {
                "admin_password": {"type": "string"},
                "admin_user": {"type": "string"},
                "db_host": {
                    "description": "Description of the db_host property",
                    "type": "string",
                },
            },
            "requirements": [
                {
                    "plugins": {
                        "node": "WordPressPlugin",
                        "occurrences": [0, "UNBOUNDED"],
                    }
                }
            ],
        },
        "WordPressPlugin": {
            "derived_from": "tosca.nodes.Root",
            "properties": {
                "name": {"type": "string"},
                "instance": {
                    "type": "string",
                    "default": {
                        "eval": "::[.type=WordPress]::.capabilities::[.name=app_endpoint]::ip_address"
                    },
                },
            },
        },
        "MyPlugin": {
            "derived_from": "WordPressPlugin",
            "properties": {"settings": {"type": "map"}},
        },
    },
    "topology_template": {},
}


def test_type_references():
    tosca_tpl = _to_yaml(type_reference_python, True)
    assert tosca_tpl == type_reference_yaml


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
default_operations_safemode_yaml = (
    default_operations_types_yaml
    + """
    interfaces:
      defaults:
        implementation: safe_mode
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
from unfurl.configurators.terraform import TerraformConfigurator, TerraformInputs
from tosca import operation, Property, Eval
from typing import Union

class unfurl_nodes_Installer_Terraform(unfurl.nodes.Installer):
    _type_name = "unfurl.nodes.Installer.Terraform"
    main: Union[str , None] = Property(metadata={"user_settable": False}, default=None)

    @operation(apply_to=["Install.check", "Standard.delete"])
    def default(self):
        return TerraformConfigurator(TerraformInputs(main=Eval({"get_property": ["SELF", "main"]})))

foo = 1
"""


def test_default_operations():
    src, src_tpl = _to_python(default_operations_yaml)
    assert "def default(self" in src
    tosca_tpl = _to_yaml(src, False)
    assert src_tpl["node_types"] == tosca_tpl["node_types"]

    tosca_tpl2 = _to_yaml(default_operations_python, False)
    assert src_tpl["node_types"] == tosca_tpl2["node_types"]

    # in safe_mode python parses ok but operations aren't executed and the yaml is missing interfaces
    tosca_tpl3 = _to_yaml(default_operations_python, True)
    assert (
        load_yaml(yaml, default_operations_safemode_yaml)["node_types"]
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

  Aliased:
    derived_from: WordPress
    metadata:
      alias: true
"""

_example_wordpress_python = '''
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

    database_endpoint: {}
    "Description of the database_endpoint requirement"

Aliased = WordPress
'''
example_wordpress_python = _example_wordpress_python.format(
    '"tosca.relationships.ConnectsTo | tosca.nodes.Database | tosca.capabilities.EndpointDatabase"'
)
example_wordpress_python_alt = _example_wordpress_python.format(
    'tosca.nodes.Database = Requirement(capability="tosca.capabilities.EndpointDatabase", relationship=tosca.relationships.ConnectsTo)'
)


def test_example_wordpress():
    src, src_tpl = _to_python(example_wordpress_yaml)
    pprint(src_tpl)
    tosca_tpl = _to_yaml(src, True)
    assert src_tpl["node_types"] == tosca_tpl["node_types"]

    tosca_tpl2 = _to_yaml(example_wordpress_python, True)
    assert src_tpl["node_types"] == tosca_tpl2["node_types"]

    tosca_tpl3 = _to_yaml(example_wordpress_python_alt, True)
    assert src_tpl["node_types"] == tosca_tpl3["node_types"]


# section 2.1 "Hello world"
example_helloworld_yaml = """
description: Template for deploying a single server with predefined properties.
topology_template:
  substitution_mappings:
    node: db_server
  node_templates:
    db_server:
      type: tosca.nodes.Compute
      metadata:
        module: service_template
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
from tosca import *  # imports GB, MB scalars, tosca_version
__root__ = tosca.nodes.Compute(
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
        version=tosca_version("6.5"),
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
example_template_yaml = """
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
      metadata:
        module: service_template
      properties:
        name: { get_input: wordpress_db_name }
        user: { get_input: wordpress_db_user }
        password: { get_input: wordpress_db_password }
      requirements:
        # test forward reference to template (python needs to reorder)
         - host: mysql

    mysql:
      type: tosca.nodes.DBMS
      metadata:
        module: service_template
"""

example_template_python = """
import tosca
from tosca import Eval

mysql = tosca.nodes.DBMS(
    "mysql",
)
wordpress_db = tosca.nodes.Database(
    "wordpress_db",
    name=Eval({"get_input": "wordpress_db_name"}),
    user=Eval({"get_input": "wordpress_db_user"}),
    password=Eval({"get_input": "wordpress_db_password"}),
    host=mysql,
)
"""

# test adding artifacts and operations that weren't declared by the type
example_operation_on_template_python = (
    example_template_python
    + """
wordpress_db.db_content = tosca.artifacts.File(file="files/wordpress_db_content.txt")
def create(self):
    return self.find_artifact("db_create.sh").execute(db_data=self.db_content)
wordpress_db.create = create
"""
)


def test_example_template():
    src, src_tpl = _to_python(example_template_yaml)
    tosca_tpl = _to_yaml(src, True)
    del src_tpl["topology_template"]["inputs"]
    assert src_tpl == tosca_tpl
    tosca_tpl2 = _to_yaml(example_template_python, True)
    assert src_tpl == tosca_tpl2
    tosca_tpl3 = _to_yaml(example_operation_on_template_python, False)
    wordpress_db = tosca_tpl3["topology_template"]["node_templates"]["wordpress_db"]
    wordpress_db["artifacts"] = {
        "db_content": {
            "type": "tosca.artifacts.File",
            "file": "files/wordpress_db_content.txt",
        }
    }
    wordpress_db["interfaces"] = {
        "Standard": {
            "type": "tosca.interfaces.node.lifecycle.Standard",
            "operations": {
                "create": {
                    "implementation": {"primary": "db_create.sh"},
                    "inputs": {"db_data": {"get_artifact": ["SELF", "db_content"]}},
                }
            },
        }
    }


custom_interface_yaml = """
tosca_definitions_version: tosca_simple_unfurl_1_0_0
node_types:
  Example:
    derived_from: tosca.nodes.Root
    properties:
      prop1:
        type: string
    artifacts:
      shellScript:
        type: tosca.artifacts.Implementation.Bash
        file: example.sh
    requirements:
    - host:
        node: tosca.nodes.Compute
    interfaces:
      MyCustomInterface:
        type: MyCustomInterface
        operations:
          my_operation:
            implementation:
              primary: shellScript
            inputs:
              location:
                eval: .::prop1
              host:
                eval: .::.targets::host::public_address
interface_types:
  MyCustomInterface:
    derived_from: tosca.interfaces.Root
    inputs: 
      location:
        type: string
      version:
        type: integer
        default: 0
    operations:
      my_operation:
        description: description of my_operation
topology_template: {}
"""


def test_custom_interface():
    class MyCustomInterface(tosca.interfaces.Root):
        class Inputs(tosca.ToscaInputs):
            location: str
            version: int = 0

        def my_operation(self):
            "description of my_operation"
            ...  # an abstract operation, subclass needs to implement

    class Example(tosca.nodes.Root, MyCustomInterface):
        shellScript = tosca.artifacts.ImplementationBash(file="example.sh")
        prop1: str
        host: tosca.nodes.Compute

        def my_operation(self):
            return self.shellScript.execute(
                MyCustomInterface.Inputs(location=self.prop1),
                host=self.host.public_address,
            )

    __name__ = "tests.test_dsl"
    converter = PythonToYaml(locals())
    yaml_dict = converter.module2yaml()
    # yaml.dump(yaml_dict, sys.stdout)
    tosca_yaml = load_yaml(yaml, custom_interface_yaml)
    assert yaml_dict == tosca_yaml


def test_bad_field():
    class BadNode(tosca.nodes.Root):
        a_property: str = tosca.Property()

    test = BadNode(a_property=test_builtin_generation)

    __name__ = "tests.test_dsl"
    converter = PythonToYaml(locals())
    with pytest.raises(TypeError):
        yaml_dict = converter.module2yaml()
        yaml.dump(yaml_dict, sys.stdout)


def test_class_init() -> None:
    class Example(tosca.nodes.Root):
        shellScript: tosca.artifacts.Root = tosca.artifacts.Root(file="example.sh")
        prop1: Optional[str] = tosca.CONSTRAINED
        host: tosca.nodes.Compute = tosca.CONSTRAINED
        self_reference: "Example" = tosca.CONSTRAINED

        @classmethod
        def _class_init(cls) -> None:
            cls.self_reference.prop1 = cls.prop1 or ""
            cls.host.public_address = cls.prop1 or ""

            with pytest.raises(ValueError) as e_info1:
                cls.host.host = cls.host.host
            assert '"host" is a capability, not a TOSCA property' in str(e_info1)

            cls.host = tosca.nodes.Compute("my_compute")
            cls.prop1 = cls.host.os.distribution
            # same as cls.host = cls.prop1 but avoids the static type mismatch error
            cls.set_to_property_source(cls.host, cls.prop1)

        def create(self, **kw) -> tosca.artifacts.Root:
            return self.shellScript.execute(input1=self.prop1)

    # print( str(inspect.signature(Example.__init__)) )

    my_template = Example("my_template")
    assert my_template._name == "my_template"
    assert my_template.prop1 == {"eval": "::my_template::prop1"}
    tosca.global_state.mode = (
        "runtime"  # test to make sure converter sets this back to spec
    )
    __name__ = "tests.test_dsl"
    converter = PythonToYaml(locals())
    yaml_dict = converter.module2yaml()
    # yaml.dump(yaml_dict, sys.stdout)
    assert yaml_dict["node_types"] == {
        "Example": {
            "derived_from": "tosca.nodes.Root",
            "properties": {
                "prop1": {
                    "type": "string",
                    "required": False,
                    "default": {
                        "eval": ".targets::host::.capabilities::[.name=os]::distribution"
                    },
                }
            },
            "artifacts": {
                "shellScript": {"file": "example.sh", "type": "tosca.artifacts.Root"}
            },
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
                    },
                },
                {
                    "self_reference": {
                        "node": "Example",
                        "node_filter": {
                            "properties": [{"prop1": {"eval": "$SOURCE::prop1"}}]
                        },
                    }
                },
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


test_datatype_yaml = """
tosca_definitions_version: tosca_simple_unfurl_1_0_0
node_types:
  Example:
    derived_from: tosca.nodes.Root
    properties:
      data:
        type: MyDataType
data_types:
  MyDataType:
    properties:
      prop1:
        type: string
        default: ''
topology_template:
  node_templates:
    test:
      type: Example
      metadata:
        module: tests.test_dsl
      properties:
        data:
          prop1: test
"""


def test_datatype():
    import tosca
    from tosca import DataType

    with tosca.set_evaluation_mode("spec"):

        class MyDataType(DataType):
            prop1: str = ""

        class Example(tosca.nodes.Root):
            data: MyDataType

        test = Example(data=MyDataType(prop1="test"))

        __name__ = "tests.test_dsl"
        converter = PythonToYaml(locals())
        yaml_dict = converter.module2yaml()
        tosca_yaml = load_yaml(yaml, test_datatype_yaml)
        # yaml.dump(yaml_dict, sys.stdout)
        assert tosca_yaml == yaml_dict


test_envvars_yaml = """
tosca_definitions_version: tosca_simple_unfurl_1_0_0
node_types:
  Example:
    derived_from: tosca.nodes.Root
    properties:
      data:
        type: MyDataType
        default:
            name: default_name
data_types:
  MyDataType:
    derived_from: unfurl.datatypes.EnvironmentVariables
    metadata:
      additionalProperties: True
      transform:
        eval:
          to_env:
            eval: $value
    properties:
      name:
        type: string
        default: default_name
topology_template:
  node_templates:
    test:
      type: Example
      metadata:
        module: tests.test_dsl
      properties:
        data:
          name: default_name
"""


def test_envvar_type():
    import tosca
    from tosca import Property, DEFAULT
    import unfurl

    with tosca.set_evaluation_mode("spec"):

        class Namespace(tosca.Namespace):
            # we can't resolve forward references to classes defined in local scope
            # (like "MyDataType" below) so we need to place them in a namespace
            class Example(tosca.nodes.Root):
                data: "MyDataType" = DEFAULT

            class MyDataType(unfurl.datatypes.EnvironmentVariables):
                name: str = "default_name"

        Example = Namespace.Example
        test = Example()

        class pcls(tosca.InstanceProxy):
            _cls = Example

        assert issubclass(pcls, Example)
        assert issubclass(Example, Example)

        p = pcls()
        assert isinstance(p, Example)
        assert isinstance(p, tosca.InstanceProxy)
        assert issubclass(type(p), Example)
        assert isinstance(test, Example)
        assert not isinstance(test, tosca.InstanceProxy)

        __name__ = "tests.test_dsl"
        converter = PythonToYaml(locals())
        yaml_dict = converter.module2yaml()
        tosca_yaml = load_yaml(yaml, test_envvars_yaml)
        # yaml.dump(yaml_dict, sys.stdout)
        assert tosca_yaml == yaml_dict


test_relationship_yaml = """
tosca_definitions_version: tosca_simple_unfurl_1_0_0
topology_template: {}
relationship_types:
  DNSRecords:
    derived_from: tosca.relationships.Root
    properties:
      records:
        type: map

node_types:
  HasDNS:
    derived_from: tosca.nodes.Root
    requirements:
      - dns:
          metadata:
            title: DNS
          node: HasDNS
          description: "DNS provider for domain name configuration"
          relationship:
            type: DNSRecords
            properties:
              records:
                "{{ '.source::.configured_by::subdomain' | eval }}":
                  type: A
                  value:
                    eval: .source::.instances::apiResource[kind=Ingress]::status::loadBalancer::ingress::0::ip
"""

test_relationship_python = '''
import tosca
from typing import Dict, Any

class DNSRecords(tosca.relationships.Root):
    records: Dict[str, Any]

_inline = DNSRecords(
    records=tosca.Eval(
        {
            "{{ '.source::.configured_by::subdomain' | eval }}": {
                "type": "A",
                "value": {
                    "eval": ".source::.instances::apiResource[kind=Ingress]::status::loadBalancer::ingress::0::ip"
                },
            }
        }
    ),
)

class HasDNS(tosca.nodes.Root):
    dns: "DNSRecords | HasDNS" = tosca.Requirement(
        default=_inline, metadata={"title": "DNS"}
    )
    """DNS provider for domain name configuration"""
'''


def test_relationship():
    python_src, parsed_yaml = _to_python(test_relationship_yaml)
    # print(python_src)
    assert parsed_yaml == _to_yaml(python_src, True)
    tosca_tpl2 = _to_yaml(test_relationship_python, True)
    # pprint(tosca_tpl2)
    assert parsed_yaml == tosca_tpl2


test_inheritance_yaml = """
tosca_definitions_version: tosca_simple_unfurl_1_0_0
topology_template: {}
node_types:
  Service:
    derived_from: tosca.nodes.Root
    attributes:
      host:
        type: string
        default: base_default
    requirements:
      - host:
          occurrences: [0, 1]
          relationship: tosca.relationships.HostedOn
          node: tosca.nodes.Abstract.Compute

  ContainerService:
    derived_from: Service
    attributes:
      host:
        type: string
        default: derived_default

  Private:
    derived_from: tosca.nodes.Root

  PrivateContainerService:
    derived_from: [Private, ContainerService]
    requirements:
      - host:
          occurrences: [1, 1] # make required
          relationship: tosca.relationships.HostedOn
          node: tosca.nodes.Compute
"""


def test_property_inheritance():
    python_src, parsed_yaml = _to_python(test_inheritance_yaml)
    assert parsed_yaml == _to_yaml(python_src, True)


@pytest.mark.parametrize(
    "test_input,exp_import,exp_path",
    [
        (dict(file="foo.yaml"), "from .foo import *", "/path/to/foo"),
        (
            dict(file="foo.yaml", namespace_prefix="ns"),
            "from . import foo as ns",
            "/path/to/foo",
        ),
        (
            dict(file="../foo.yaml", namespace_prefix="ns"),
            "from .. import foo as ns",
            "/path/foo",
        ),
        (
            dict(file="foo.yaml", namespace_prefix="foo"),
            "from . import foo",
            "/path/to/foo",
        ),
        (dict(file="../foo.yaml"), "from ..foo import *", "/path/foo"),
        (
            dict(file="../../foo.yaml"),
            "from ...foo import *",
            "/foo",
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
            dict(repository="repo", file="foo.yaml", namespace_prefix="dotted.ns"),
            "from tosca_repositories.repo import foo as dotted_ns",
            "tosca_repositories/repo/foo",
        ),
        (
            dict(
                repository="repo", file="subdir/foo.yaml", namespace_prefix="dotted.ns"
            ),
            "from tosca_repositories.repo.subdir import foo as dotted_ns",
            "tosca_repositories/repo/subdir/foo",
        ),
        (
            dict(repository="repo", file="foo.yaml", namespace_prefix="ns"),
            "from tosca_repositories.repo import foo as ns",
            "tosca_repositories/repo/foo",
        ),
        (
            dict(repository="repo", file="foo.yaml", namespace_prefix="foo"),
            "from tosca_repositories.repo import foo",
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
        c = tosca.yaml2python.Convert(MagicMock(path="/path/to/including_file.yaml"))
        output = c.convert_import(test_input)

        # generated import
        assert output[0].strip() == exp_import
        # import path
        assert output[1] == exp_path


def test_sandbox(capsys):
    _to_yaml("print('hello', 'world')", True)
    captured = capsys.readouterr()
    assert captured.out == "hello world"

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
        "import tosca_repositories.missing_repository",
        "from tosca._tosca import global_state; foo = global_state; foo.safe_mode",
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
tosca.nodes.Root._type_name = 'pown'""",
        """from unfurl.support import to_label; to_label('d')""",
    ]
    for src in denied:
        # misc errors: SyntaxError, NameError, TypeError
        with pytest.raises(Exception):
            assert _to_yaml(src, True)

    allowed = [
        """foo = {}; foo[1] = 2; bar = []; bar.append(1); baz = ()""",
        """foo = dict(); foo[1] = 2; bar = list(); bar.append(1); baz = tuple()""",
        """import math; math.floor(1.0)""",
        """from unfurl.configurators.templates.dns import unfurl_relationships_DNSRecords""",
        """from unfurl.tosca_plugins import k8s; k8s.kube_artifacts""",
        """import tosca
node = tosca.nodes.Root()
node._name = "test"
        """,
    ]
    for src in allowed:
        assert _to_yaml(src, True)


def test_write_policy():
    test_path = os.path.join(os.getenv("UNFURL_TMPDIR"), "test_generated.txt")
    src = "import unfurl\n"
    with open(test_path, "w") as f:
        f.write(tosca.WritePolicy.auto.generate_comment("test", "source_file") + src)
    try:
        # hasn't changed so it can't be overwritten
        assert tosca.WritePolicy.auto.can_overwrite("ignore", test_path)
        can_write, unchanged = tosca.WritePolicy.auto.can_overwrite_compare(
            "ignore", test_path, src + "# ignore\n#\n"
        )
        assert can_write, unchanged == (True, True)
        assert (
            tosca.WritePolicy.auto.deny_message(unchanged)
            == 'overwrite policy is "auto" but the contents have not changed'
        )
        can_write, unchanged = tosca.WritePolicy.auto.can_overwrite_compare(
            "ignore", test_path, "# ignore\nimport tosca\n"
        )
        assert can_write, unchanged == (True, False)
        # mark the output file as modified after the time recorded in the comment
        os.utime(test_path, (time.time() + 5, time.time() + 5))
        # it's changed, so don't overwrite it
        assert not tosca.WritePolicy.auto.can_overwrite("ignore", test_path)
    finally:
        os.remove(test_path)


def test_export():
    runner = CliRunner()
    path = os.path.join(
        os.path.dirname(__file__), "examples", "helm-simple-ensemble.yaml"
    )
    assert tosca.global_state.mode != "runtime"
    result = runner.invoke(
        cli,
        [
            "export",
            "--format",
            "python",
            "--python-target",
            "3.7",
            "--overwrite",
            "always",
            path,
        ],
    )
    # print(result.stdout)
    if result.exception:
        raise result.exception
    assert result.exit_code == 0


if __name__ == "__main__":
    _generate_builtin(
        yaml2python.generate_builtins, "tosca-package/tosca/builtin_types"
    )
    _generate_builtin(
        yaml2python.generate_builtin_extensions, "unfurl/tosca_plugins/tosca_ext"
    )
    # regenerate template modules:
    yaml2python.Convert.convert_built_in = True
    path = os.path.abspath(os.path.dirname(unfurl.__file__))
    yaml_src = f"""
    repositories:
      unfurl:
        url: file:{path}
    imports:
      - repository: unfurl
        file: configurators/templates/helm.yaml
      - repository: unfurl
        file: configurators/templates/dns.yaml
      - repository: unfurl
        file: configurators/templates/docker.yaml
      - repository: unfurl
        file: tosca_plugins/artifacts.yaml
      - repository: unfurl
        file: tosca_plugins/k8s.yaml
      - repository: unfurl
        file: tosca_plugins/googlecloud.yaml
    """
    manifest = Manifest(path)
    _to_python(yaml_src, 7, tosca.WritePolicy.always, manifest)
