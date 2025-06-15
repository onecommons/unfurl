import dataclasses
import inspect
import os
import time
from typing import Any, List, Optional, Dict, Sequence, Union
import typing
import unittest
from unittest.mock import MagicMock, patch
import pytest
from pprint import pprint
import unfurl  # must be before tosca imports
from tosca import OpenDataEntity
from unfurl.merge import diff_dicts
from unfurl.dsl import get_allowed_modules
import sys
import copy
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
from unfurl.configurators.templates.docker import unfurl_datatypes_DockerContainer
import tosca


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
            convert_repositories=True,
        )
    finally:
        globals._annotate_namespaces = current
    return src, tosca_yaml


def _to_yaml(python_src: str, safe_mode) -> dict:
    namespace: dict = {}
    current = globals._annotate_namespaces
    try:
        globals._annotate_namespaces = False
        if safe_mode:
            modules = get_allowed_modules()
        else:
            modules = None
        tosca_tpl = python_src_to_yaml_obj(
            python_src, namespace, safe_mode=safe_mode, modules=modules
        )
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
    return _to_yaml(python_src, False)


def test_builtin_generation():
    yaml_src = _generate_builtin(yaml2python.generate_builtins)
    src_yaml = EntityType.TOSCA_DEF_LOAD_AS_IS
    for section in EntityType.TOSCA_DEF_SECTIONS:
        if section == "types":
            continue
        print(section)
        assert len(list(src_yaml[section])) == len(list(yaml_src[section])), (
            section,
            list(src_yaml[section]),
            list(yaml_src[section]),
        )
        diffs = diff_dicts(
            src_yaml[section], yaml_src[section], skipkeys=("description", "required")
        )
        diffs.pop("unfurl.interfaces.Install", None)
        for sectiontype in ["nodes", "relationships", "groups"]:
            diffs.pop(
                f"tosca.{sectiontype}.Root", None
            )  # adds "type" to Standard interface
        diffs.pop(
            "tosca.nodes.SoftwareComponent", None
        )  # !namespace attributes might get added by other tests
        print(yaml2python.value2python_repr(diffs))
        if diffs:
            # these diffs exist because requirements include inherited types and aliased block and object storaged types
            assert section == "node_types" and len(diffs) == 8


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
    # pprint(src_tpl)
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
      interfaces:
        Standard:
          operations:
            configure: echo "abbreviated configuration"
"""

example_helloworld_python = '''
"""Template for deploying a single server with predefined properties."""
import tosca
from tosca import *  # imports GB, MB scalars, tosca_version
from typing import Any

@operation(name="configure")
def db_server_configure(**kw: Any) -> Any:
    return unfurl.configurators.shell.ShellConfigurator(
        command='echo "abbreviated configuration"',
    )

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
__root__.configure = db_server_configure
'''


def test_example_helloworld():
    src, src_tpl = _to_python(example_helloworld_yaml)
    tosca_tpl = _to_yaml(src, True)
    src_tpl["topology_template"]["node_templates"]["db_server"]["interfaces"][
        "Standard"
    ] = {
        "operations": {"configure": {"implementation": "safe_mode"}},
    }
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
from tosca import Eval, TopologyInputs

class Inputs(TopologyInputs):
    wordpress_db_name: str
    wordpress_db_user: str
    wordpress_db_password: str

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
wordpress_db.set_operation(create)
"""
)


def test_example_template():
    src, src_tpl = _to_python(example_template_yaml)
    tosca_tpl = _to_yaml(src, True)
    assert src_tpl == tosca_tpl
    tosca_tpl2 = _to_yaml(example_template_python, True)
    assert src_tpl == tosca_tpl2
    tosca_tpl3 = _to_yaml(example_operation_on_template_python, False)
    wordpress_db = tosca_tpl3["topology_template"]["node_templates"]["wordpress_db"]
    assert wordpress_db.get("artifacts") == {
        "db_content": {
            "type": "tosca.artifacts.File",
            "file": "files/wordpress_db_content.txt",
        }
    }, yaml.dump(tosca_tpl3, sys.stdout) or "unexpected yaml, see stdout"
    assert wordpress_db["interfaces"] == {
        "Standard": {
            "operations": {
                "create": {
                    "implementation": {"primary": "db_create.sh"},
                    "inputs": {"db_data": {"get_artifact": ["SELF", "db_content"]}},
                    "metadata": {
                        "arguments": [
                            "db_data",
                        ],
                    },
                }
            },
        }
    }, yaml.dump(tosca_tpl3, sys.stdout) or "unexpected yaml, see stdout"


custom_interface_python = """
import unfurl
import tosca
class MyCustomInterface(tosca.interfaces.Root):
    class Inputs(tosca.ToscaInputs):
        location: str
        version: int = 0

    my_operation = tosca.operation()
    "an abstract operation, subclass needs to implement"

class Example(tosca.nodes.Root, MyCustomInterface):
    shellScript = tosca.artifacts.ImplementationBash(file="example.sh")
    prop1: str
    host: tosca.nodes.Compute

    def my_operation(self):
        return self.shellScript.execute(
            MyCustomInterface.Inputs(location=self.prop1),
            host=self.host.public_address,
        )
"""

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
            metadata:
              arguments: [location, host]
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
        description: an abstract operation, subclass needs to implement
topology_template: {}
"""


def test_custom_interface():
    yaml_dict = _to_yaml(custom_interface_python, False)
    yaml.dump(yaml_dict, sys.stdout)
    tosca_yaml = load_yaml(yaml, custom_interface_yaml)
    assert yaml_dict == tosca_yaml


def test_set_operations():
    python_src = """
import unfurl
import tosca
cmd = unfurl.artifacts.ShellExecutable(file="cmd.sh")
task = tosca.set_operations(configure=cmd)
    """
    yaml_src = """
tosca_definitions_version: tosca_simple_unfurl_1_0_0
topology_template:
  node_templates:
    task:
      type: tosca.nodes.Root
      interfaces:
        Standard:
          operations:
            configure:
              implementation:
                primary:
                  type: unfurl.artifacts.ShellExecutable
                  file: cmd.sh
      metadata:
        module: service_template
"""
    yaml_dict = _to_yaml(python_src, False)
    # yaml.dump(yaml_dict, sys.stdout)
    tosca_yaml = load_yaml(yaml, yaml_src)
    assert yaml_dict == tosca_yaml


def test_node_filter():
    python_src = """
import unfurl
import tosca
class Base(tosca.nodes.Root):
    req: tosca.nodes.Root = tosca.Requirement(node_filter={"match": [dict(get_nodes_of_type="MyNode")]})

class Derived(Base):
    req: tosca.nodes.Compute

test = Derived()
    """
    yaml_src = """
tosca_definitions_version: tosca_simple_unfurl_1_0_0
topology_template:
  node_templates:
    test:
      type: Derived
      metadata:
        module: service_template
node_types:
  Base:
    derived_from: tosca.nodes.Root
    requirements:
    - req:
        node: tosca.nodes.Root
        node_filter:
          match: 
          - get_nodes_of_type: MyNode
  Derived:
    derived_from: Base
    requirements:
    - req:
        node: tosca.nodes.Compute
"""
    yaml_dict = _to_yaml(python_src, False)
    tosca_yaml = load_yaml(yaml, yaml_src)
    assert yaml_dict == tosca_yaml, (
        yaml.dump(yaml_dict, sys.stdout) or "unexpected yaml, see stdout"
    )


def test_node_filter_on_template():
    python_src = """
import unfurl
import tosca
class Host(tosca.nodes.Root):
    pass

class App(tosca.nodes.Root):
    host: Host

foo = App(host=tosca.CONSTRAINED)
test = App(host=foo._find_template(".hosted_on", Host))
"""
    yaml_src = """
tosca_definitions_version: tosca_simple_unfurl_1_0_0
topology_template:
  node_templates:
    foo:
      type: App
      requirements:
      - host:
          node: Host
      metadata:
        module: service_template
    test:
      type: App
      requirements:
      - host:
          node_filter:
            match:
            - eval: ::foo::.hosted_on::[.type=Host]
      metadata:
        module: service_template
node_types:
  Host:
    derived_from: tosca.nodes.Root
  App:
    derived_from: tosca.nodes.Root
    requirements:
    - host:
        node: Host
"""
    # make sure that the node_filter is only on the template not the node type
    yaml_dict = _to_yaml(python_src, False)
    tosca_yaml = load_yaml(yaml, yaml_src)
    assert yaml_dict == tosca_yaml, (
        yaml.dump(yaml_dict, sys.stdout) or "unexpected yaml, see stdout"
    )


def test_generator_operation():
    import unfurl.configurators.shell
    from unfurl.configurator import TaskView

    class Node(tosca.nodes.Root):
        def foo(self, ctx: TaskView):
            result = yield unfurl.configurators.shell.ShellConfigurator()
            if result.result.success:
                return ctx.done()

        def configure(self, **kw):
            return self.foo

    test = Node()

    __name__ = "tests.test_dsl"
    converter = PythonToYaml(locals())
    yaml_dict = converter.module2yaml()
    yaml.dump(yaml_dict, sys.stdout)


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
        shellScript: tosca.artifacts.Root = (
            tosca.CONSTRAINED
        )  # set in _class_init, _post_init raise error if still CONSTRAINED
        prop1: Optional[str] = (
            tosca.CONSTRAINED
        )  # set in _class_init, _post_init raise error if still CONSTRAINED
        # CONSTRAINED requirements are shared on type; if not set or constrained in _class_init, defaults to type constrain (set on the template's requirement)
        host: tosca.nodes.Compute = tosca.CONSTRAINED
        host2: tosca.nodes.Compute = (
            tosca.DEFAULT
        )  # new template created for each instance
        self_reference: "Example" = tosca.CONSTRAINED
        prop2: Optional[str] = None
        prop3: str = tosca.DEFAULT

        @classmethod
        def _class_init(cls) -> None:
            cls.self_reference.prop1 = cls.prop1 or ""
            cls.host.public_address = cls.prop1 or ""
            cls.shellScript = tosca.artifacts.Root(file="example.sh", intent=cls.prop1)

            with pytest.raises(ValueError) as e_info1:
                cls.host.host = cls.host.host
            assert '"host" is a capability, not a TOSCA property' in str(e_info1)

            # set host requirement but the node_filter set above still applies,
            # (acting as a validation check on the node)
            cls.host = tosca.nodes.Compute("my_compute")
            cls.prop1 = cls.host.os.distribution
            # same as cls.host = cls.prop1 but avoids the static type mismatch error
            cls.set_to_property_source(cls.host, cls.prop1)
            cls.prop2 = cls.host._name  # XXX tosca_name is shadowed
            cls.prop3 = cls._name

        def create(self, **kw) -> tosca.artifacts.Root:
            self.shellScript.execute(input1=self.prop1)
            return self.shellScript

    # print( str(inspect.signature(Example.__init__)) )

    my_template = Example("my_template")
    assert my_template._name == "my_template"
    assert my_template.prop1 == {"eval": "::my_template::prop1"}

    my_template2 = Example()
    with pytest.raises(
        ValueError,
        match='"host" is a type constraint, can not be modified by individual templates',
    ):
        # template returns field projection if CONSTRAINED and its __setattr__ raises ValueError
        my_template2.host.public_address = (
            "1.1.1.1"  # instance modifying a constrained field is error
        )

    my_template3 = Example()
    my_template3.host = tosca.nodes.Compute()  # override class constraint
    # template returns the assigned node, not a field projection, so it's ok to modify
    my_template3.host.public_address = "1.1.1.1"  # ok

    tosca.global_state.mode = (
        "runtime"  # test to make sure converter sets this back to spec
    )
    __name__ = "tests.test_dsl"
    converter = PythonToYaml(locals())
    yaml_dict = converter.module2yaml()
    assert yaml_dict["topology_template"] == {
        "node_templates": {
            "my_compute": {
                "type": "tosca.nodes.Compute",
                "metadata": {
                    "module": "tests.test_dsl",
                },
            },
            "my_template": {
                "type": "Example",
                "requirements": [{"host2": "my_template_host2"}],
                "metadata": {"module": "tests.test_dsl"},
            },
            "my_template_host2": {
                "type": "tosca.nodes.Compute",
                "directives": [
                    "dependent",
                ],
                "metadata": {
                    "module": "tests.test_dsl",
                },
            },
            "my_template2": {
                "type": "Example",
                "requirements": [{"host2": "my_template2_host2"}],
                "metadata": {"module": "tests.test_dsl"},
            },
            "my_template2_host2": {
                "type": "tosca.nodes.Compute",
                "directives": [
                    "dependent",
                ],
                "metadata": {
                    "module": "tests.test_dsl",
                },
            },
            "my_template3": {
                "type": "Example",
                "requirements": [
                    {"host": "my_template3_host"},
                    {"host2": "my_template3_host2"},
                ],
                "metadata": {"module": "tests.test_dsl"},
            },
            "my_template3_host": {
                "type": "tosca.nodes.Compute",
                "attributes": {"public_address": "1.1.1.1"},
                "directives": [
                    "dependent",
                ],
                "metadata": {
                    "module": "tests.test_dsl",
                },
            },
            "my_template3_host2": {
                "type": "tosca.nodes.Compute",
                "directives": [
                    "dependent",
                ],
                "metadata": {
                    "module": "tests.test_dsl",
                },
            },
        }
    }, yaml.dump(yaml_dict, sys.stdout) or "unexpected yaml, see stdout"
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
                },
                "prop2": {
                    "default": {
                        "eval": ".targets::host::.name",
                    },
                    "required": False,
                    "type": "string",
                },
                "prop3": {
                    "default": {
                        "eval": ".name",
                    },
                    "type": "string",
                },
            },
            "artifacts": {
                "shellScript": {
                    "file": "example.sh",
                    "type": "tosca.artifacts.Root",
                    "intent": {
                        "eval": ".owner::prop1",
                    },
                }
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
                    "host2": {
                        "node": "tosca.nodes.Compute",
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
                            "metadata": {
                                "arguments": [
                                    "input1",
                                ],
                            },
                        }
                    }
                }
            },
        }
    }, yaml.dump(yaml_dict, sys.stdout) or "unexpected yaml, see stdout"


template_init_yaml = """
tosca_definitions_version: tosca_simple_unfurl_1_0_0
topology_template:
  node_templates:
    e0:
      type: Example
      artifacts:
        shellScript:
          type: tosca.artifacts.Root
          file: example.sh
          intent:
            eval: ::e0::prop1::key::0
      properties:
        prop1:
          not default:
          - test
      requirements:
      - host: e0_host # created in _template_init
      metadata:
        module: tests.test_dsl
    e0_host:
      type: tosca.nodes.Compute
      directives:
      - dependent
      metadata:
        module: tests.test_dsl
    e1:
      type: Example
      artifacts:
        shellScript:
          type: tosca.artifacts.Root
          file: example.sh
          intent:
            eval: ::e1::prop1::key::0
      properties:
        prop1:
          key:
          - test
      requirements:
      - host:  # customized requirement with constraint instead of template
          node: tosca.nodes.Compute
          relationship: tosca.relationships.HostedOn
          metadata:
            test: just some metadata but not a node
      metadata:
        module: tests.test_dsl
    e2:
      type: Example
      artifacts:
        shellScript:
          type: tosca.artifacts.Root
          file: example.sh
          intent:
            eval: ::e2::prop1::key::0
      requirements:
      - host: mm  # constructor overrode __template_init
      metadata:
        module: tests.test_dsl
    mm:
      type: tosca.nodes.Compute
      metadata:
        module: tests.test_dsl
    e3:
      type: Example
      artifacts:
        shellScript:
          type: tosca.artifacts.Root
          file: example.sh
      # explicitly declared "host" unset but Example doesn't have a node_filter or value to unset
      # so no requirement is generated
      metadata:
        module: tests.test_dsl
node_types:
  Example:
    derived_from: tosca.nodes.Root
    artifacts:
      shellScript:
        type: tosca.artifacts.Root
    properties:
      prop1:
        type: map
        entry_schema:
          type: list
          entry_schema:
            type: string
        default: {}
    requirements:
    - host:
        node: tosca.nodes.Compute
        relationship: tosca.relationships.HostedOn
"""


def test_has_default() -> None:
    shared = tosca.nodes.Compute()

    class HasDefault(tosca.nodes.Root):
        default: Dict[str, List[str]] = tosca.DEFAULT
        constrained: Union[tosca.nodes.Compute, tosca.relationships.HostedOn] = (
            tosca.CONSTRAINED
        )
        none: Optional[str] = None
        requirement: tosca.nodes.Compute = shared
        field: tosca.artifacts.Root = tosca.Artifact(default=tosca.artifacts.Root())
        field2: str = tosca.Property(factory=lambda: "default")

        def _template_init(self) -> None:
            for name in [
                "default",
                "constrained",
                "none",
                "requirement",
                "field",
                "field2",
            ]:
                if "not_set" in self._name:
                    assert self.has_default(name)
                else:
                    assert not self.has_default(name)

    HasDefault("not_set")
    HasDefault(
        "not_set_with_defaults",
        default=tosca.DEFAULT,
        constrained=tosca.CONSTRAINED,
        none=None,
        requirement=shared,
        field=tosca.DEFAULT,
        field2=tosca.DEFAULT,
    )  # note: field2="default" won't be has_default()
    HasDefault(
        "not_set_with_fields",
        default=tosca.Property(default=tosca.DEFAULT),
        constrained=tosca.Requirement(default=tosca.CONSTRAINED),
        none=tosca.Property(default=None),
        requirement=tosca.Requirement(default=shared),
        field=tosca.Artifact(default=tosca.DEFAULT),
        field2=tosca.Property(default=tosca.DEFAULT),
    )
    HasDefault(
        "set",
        default={"key": ["value"]},
        constrained=tosca.relationships.HostedOn(),
        none="set",
        requirement=tosca.nodes.Compute("not_shared"),
        field=tosca.artifacts.Root(),
        field2="set",
    )
    HasDefault(
        "set_with_field",
        default=tosca.Property(factory=lambda: {"key": ["value"]}),
        constrained=tosca.Requirement(default=tosca.nodes.Compute("set")),
        none=tosca.Property(default="set"),
        requirement=tosca.Requirement(default=tosca.nodes.Compute("not_shared")),
        field=tosca.Artifact(default=tosca.artifacts.Root("set")),
        field2=tosca.Property(default="set"),
    )


def test_template_init() -> None:
    class Example(tosca.nodes.Root):
        shellScript: tosca.artifacts.Root = tosca.REQUIRED  # equivalent to no default, use if _template_init will set (but you lose static type checking)
        prop1: Dict[str, List[str]] = tosca.DEFAULT
        host: Union[tosca.nodes.Compute, tosca.relationships.HostedOn] = (
            tosca.CONSTRAINED
        )

        def _template_init(self) -> None:
            if self.has_default("shellScript"):
                if self.host:
                    intent = self.prop1["key"][0]
                else:
                    intent = None  # self.host is None on template e3
                self.shellScript = tosca.artifacts.Root(
                    file="example.sh", intent=intent
                )
            if self.has_default(self.__class__.host):
                # XXX ensure that template names are unique per instance
                self.host = tosca.nodes.Compute()

    # ok to construct without required arguments because _template_init() sets them
    e0 = Example()
    e0.prop1 = {"not default": ["test"]}

    # adds a constraint to override the default node with type inference:
    e1 = Example()
    # we want to reset this to CONSTRAINED so need to do that after init because has_default() thinks its the default
    e1.host = tosca.Requirement(
        default=tosca.CONSTRAINED,
        metadata={"test": "just some metadata but not a node"},
    )
    e1.prop1["key"] = ["test"]
    assert e1.prop1["key"] == ["test"]

    e2 = Example(host=tosca.nodes.Compute("mm"))
    # no node and no constraint:
    e3 = Example(host=tosca.Requirement(default=None))

    __name__ = "tests.test_dsl"
    converter = PythonToYaml(locals())
    yaml_dict = converter.module2yaml()
    expected = load_yaml(yaml, template_init_yaml)
    assert yaml_dict == expected, (
        yaml.dump(yaml_dict, sys.stdout) or "unexpected yaml, see stdout"
    )


PATCH = tosca.PATCH

test_patching_yaml = """
tosca_definitions_version: tosca_simple_unfurl_1_0_0
topology_template:
  node_templates:
    named:
      type: Service
      properties:
        container:
          environment:
            VAR1: a
            VAR2: a
          command:
          - a
          - b
          - c
        foo: foo
      metadata:
        module: tests.test_dsl
    app:
      type: App
      metadata:
        module: tests.test_dsl
    app2:
      type: App
      requirements:
      - service: app2_service
      metadata:
        module: tests.test_dsl
    app2_service:
      type: Service
      properties:
        container:
          environment:
            P1: p
            VAR2:
            VAR1: a
          command:
          - a
          - b
          - c
          extra: extra
        overlays:
          overlay: 1
        foo: foo
      directives:
      - dependent
      metadata:
        module: tests.test_dsl
node_types:
  App:
    derived_from: tosca.nodes.Root
    requirements:
    - service:
        node: named
  Service:
    derived_from: tosca.nodes.Root
    properties:
      container:
        type: unfurl.datatypes.DockerContainer
        default: {}
      overlays:
        type: map
        default: {}
      foo:
        type: string
        required: false
"""


def test_patching(mocker):
    class Service(tosca.nodes.Root):
        container: unfurl_datatypes_DockerContainer = tosca.DEFAULT
        overlays: Dict[str, Any] = tosca.DEFAULT
        foo: Optional[str] = None

        def _template_init(self):
            if self.has_default("container"):  # true even if initialized with PATCH
                self.container = unfurl_datatypes_DockerContainer(
                    command=["a", "b", "c"],
                    environment=unfurl.datatypes.EnvironmentVariables(
                        VAR1="a", VAR2="a"
                    ),
                )

    class App(tosca.nodes.Root):
        service: Service = Service("named", foo="foo")

    spy_remove_patches = mocker.spy(App, "_remove_patches")
    spy_merge = mocker.spy(App, "_merge")

    container = unfurl_datatypes_DockerContainer(
        PATCH,
        environment=unfurl.datatypes.EnvironmentVariables(PATCH, P1="p", VAR2=None),
    )
    container.extend(extra="extra")
    app = App()  # create an empty app to make sure shared service wasn't modified
    app2 = App(
        service=Service(
            PATCH,
            overlays={"overlay": 1},
            container=container,
        ),
    )
    assert len(spy_remove_patches.spy_return) == 1
    assert spy_remove_patches.spy_return["service"]._name == PATCH
    assert spy_merge.spy_return.overlays == {"overlay": 1}
    assert app2.service.container.extra == "extra"
    assert app2.service.container.command == ["a", "b", "c"]
    assert app2.service.container.environment.P1 == "p"
    assert app2.service.container.environment.VAR1 == "a"

    __name__ = "tests.test_dsl"
    converter = PythonToYaml(locals())
    yaml_dict = converter.module2yaml()
    tosca_yaml = load_yaml(yaml, test_patching_yaml)
    assert tosca_yaml == yaml_dict, (
        yaml.dump(yaml_dict, sys.stdout) or "unexpected yaml, see stdout"
    )


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
      map:
        type: map
        default: {}
        entry_schema:
          type: integer
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
    from tosca import DataEntity, DEFAULT

    with tosca.set_evaluation_mode("parse"):

        class MyDataType(DataEntity):
            prop1: str = ""
            map: Dict[str, int] = DEFAULT

        class Example(tosca.nodes.Root):
            data: MyDataType

        test = Example(data=MyDataType(prop1="test"))

        __name__ = "tests.test_dsl"
        converter = PythonToYaml(locals())
        yaml_dict = converter.module2yaml()
        tosca_yaml = load_yaml(yaml, test_datatype_yaml)
        assert tosca_yaml == yaml_dict, (
            yaml.dump(yaml_dict, sys.stdout) or "unexpected yaml, see stdout"
        )


test_envvars_yaml = """
tosca_definitions_version: tosca_simple_unfurl_1_0_0
node_types:
  Example:
    derived_from: tosca.nodes.Root
    properties:
      data:
        type: MyDataType
        default: {}
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
    open_test:
      type: Example
      properties:
        more: 1
      metadata:
        module: tests.test_dsl
"""


def test_envvar_type():
    import tosca
    from tosca import Property, DEFAULT
    import unfurl
    from unfurl.configurators.templates.docker import unfurl_datatypes_DockerContainer

    with tosca.set_evaluation_mode("parse"):

        class Namespace(tosca.Namespace):
            # we can't resolve forward references to classes defined in local scope
            # (like "MyDataType" below) so we need to place them in a namespace
            class Example(tosca.nodes.Root):
                data: "MyDataType" = DEFAULT

            class MyDataType(unfurl.datatypes.EnvironmentVariables):
                name: str = "default_name"

        Example = Namespace.Example
        test = Example()

        open_test = Example()
        open_test.more = 1

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
        assert tosca_yaml == yaml_dict, (
            yaml.dump(yaml_dict, sys.stdout) or "unexpected yaml, see stdout"
        )

        generic_envvars = unfurl.datatypes.EnvironmentVariables(DBASE="aaaa", URL=True)
        assert generic_envvars != unfurl.datatypes.EnvironmentVariables()
        assert generic_envvars == unfurl.datatypes.EnvironmentVariables(
            DBASE="aaaa", URL=True
        )
        generic_envvars.MORE = 1
        assert generic_envvars.DBASE == "aaaa"
        assert generic_envvars.MORE == 1
        assert generic_envvars == unfurl.datatypes.EnvironmentVariables(
            DBASE="aaaa", URL=True, MORE=1
        )
        assert generic_envvars != unfurl.datatypes.EnvironmentVariables()
        assert (
            unfurl.datatypes.EnvironmentVariables()
            == unfurl.datatypes.EnvironmentVariables()
        )

        cloned = copy.copy(generic_envvars)
        assert cloned == generic_envvars
        assert cloned.URL == True
        assert cloned.DBASE == "aaaa"
        assert cloned.MORE == 1
        cloned.another = 1
        assert "another" in cloned._instance_fields
        assert "another" not in generic_envvars._instance_fields
        assert cloned != generic_envvars

        assert generic_envvars.to_yaml() == {"DBASE": "aaaa", "URL": True, "MORE": 1}
        assert OpenDataEntity(a=1, b="b").extend(c="c").to_yaml() == {
            "a": 1,
            "b": "b",
            "c": "c",
        }
        assert Namespace.MyDataType(name="foo").to_yaml() == {"name": "foo"}
        # make sure DockerContainer is an OpenDataEntity
        unfurl_datatypes_DockerContainer().extend(labels=dict(foo="bar"))


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


test_simple_datatype_yaml = """
tosca_definitions_version: tosca_simple_unfurl_1_0_0
data_types:
  MyStringType:
    type: string
node_types:
  MyNode:
    derived_from: tosca.nodes.Root
    properties:
      prop1:
        type: MyStringType
      map:
        type: map
        default: {}
        entry_schema:
          type: MyStringType
topology_template:
  node_templates:
    a_template:
      type: MyNode
      metadata: {'module': 'service_template'}
      properties:
        prop1: foo
"""

test_simple_datatype_python = """
import unfurl
from typing import List, Dict, Any, Tuple, Union, Sequence
import tosca
from tosca import DataEntity, Node, Property


class MyStringType(tosca.ValueType, str):
    pass


class MyNode(tosca.nodes.Root):
    prop1: MyStringType
    map: Dict[str, "MyStringType"] = Property(factory=lambda: ({}))

a_template = MyNode(prop1=MyStringType("foo"))

__all__ = ["MyStringType", "MyNode"]
"""


def test_simple_datatype():
    python_src, parsed_yaml = _to_python(test_simple_datatype_yaml)
    # print(python_src)
    parsed_yaml2 = _to_yaml(python_src, True)
    assert parsed_yaml == parsed_yaml
    tosca_tpl2 = _to_yaml(test_simple_datatype_python, True)
    # pprint(tosca_tpl2)
    assert parsed_yaml == tosca_tpl2


test_group_yaml = """
tosca_definitions_version: tosca_simple_unfurl_1_0_0
topology_template:
  node_templates:
    n:
      type: MyNode
      metadata:
        module: tests.test_dsl
  groups:
    g:
      type: MyGroup
      members:
      - n
      metadata:
        module: tests.test_dsl
  policies:
    my_policy:
      type: tosca.policies.Root
      targets:
      - g
      metadata:
        module: tests.test_dsl
group_types:
  MyGroup:
    derived_from: tosca.groups.Root
    members:
    - MyNode
node_types:
  MyNode:
    derived_from: tosca.nodes.Root
"""

test_repository_python = """
import tosca
from unfurl.tosca_plugins import expr
std = tosca.Repository("https://unfurl.cloud/onecommons/std",
                          credential=tosca.datatypes.Credential(user="a_user", token=expr.get_env("MY_TOKEN", None)))
"""

test_repository_yaml = """
tosca_definitions_version: tosca_simple_unfurl_1_0_0
topology_template: {}
repositories:
  std:
    url: https://unfurl.cloud/onecommons/std
    credential:
      token:
        get_env: ["MY_TOKEN", null]
      user: a_user
"""


def test_repository():
    src, src_tpl = _to_python(test_repository_yaml)
    parsed_yaml = _to_yaml(test_repository_python, True)
    assert src_tpl == parsed_yaml
    tosca_tpl2 = _to_yaml(src, True)
    assert tosca_tpl2 == parsed_yaml


def test_groups_and_policies():
    class MyNode(tosca.nodes.Root):
        pass

    n = MyNode()

    class MyGroup(tosca.groups.Root):  # type: ignore[override]
        _members: Sequence[MyNode] = ()  # type: ignore[override]

    g = MyGroup(_members=[n])
    my_policy = tosca.policies.Root(_targets=[g])

    __name__ = "tests.test_dsl"
    converter = PythonToYaml(locals())
    yaml_dict = converter.module2yaml()
    tosca_yaml = load_yaml(yaml, test_group_yaml)
    # yaml.dump(yaml_dict, sys.stdout)
    assert tosca_yaml == yaml_dict


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
    assert parsed_yaml == _to_yaml(python_src, True), python_src


test_deploymentblueprint_yaml = """
tosca_definitions_version: tosca_simple_unfurl_1_0_0
node_types:
  Node:
    derived_from: tosca.nodes.Root
    requirements:
    - another:
        node: Node
        occurrences:
        - 0
        - 1
topology_template:
  node_templates:
    node:
      type: Node
      metadata:
        module: service_template
    node2:
      type: Node
      requirements:
      - another: node
      metadata:
        module: service_template
deployment_blueprints:
  production:
    cloud: unfurl.relationships.ConnectsTo.AWSAccount
    node_templates:
      node:
        type: Node
        requirements:
        - another: node2
        metadata:
          module: service_template
  dev:
    cloud: unfurl.relationships.ConnectsTo.K8sCluster
    node_templates:
      node3:
        type: Node
        requirements:
        - another: node2
        metadata:
          module: service_template
      node:
        type: Node
        requirements:
        - another: node3
        metadata:
          module: service_template
"""

test_deploymentblueprint_python = """
import tosca
from typing import Optional

class Node(tosca.nodes.Root):
    another: Optional["Node"] = None

node = Node("node")
node2 = Node(another=node)

class production(tosca.DeploymentBlueprint):
    _cloud = "unfurl.relationships.ConnectsTo.AWSAccount"

    node = Node(another=node2)

class dev(tosca.DeploymentBlueprint):
    _cloud = "unfurl.relationships.ConnectsTo.K8sCluster"

    node3 = Node(another=node2)
    node = Node(another=node3)
"""


def test_deployment_blueprints():
    python_src, parsed_yaml = _to_python(test_deploymentblueprint_yaml)
    # print(python_src)
    # pprint(parsed_yaml)
    test_yaml = _to_yaml(python_src, True)
    # yaml.dump(test_yaml, sys.stdout)
    assert parsed_yaml == test_yaml
    tosca_tpl2 = _to_yaml(test_deploymentblueprint_python, True)
    # yaml.dump(tosca_tpl2, sys.stdout)
    assert parsed_yaml == tosca_tpl2


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
        (
            dict(repository="repo", file="__init__.yaml", namespace_prefix="repo"),
            "from tosca_repositories import repo",
            "tosca_repositories/repo/__init__",
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
        "from tosca_repositories import missing_repository",
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
        # print("should deny:\n", src)
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
        "import urllib.parse; import urllib; urllib.request",
        "import random; getattr(random, '_os')",
        "import os.path; os.path.exists('foo/bar')",  # not a safe function
        "import unfurl; unfurl._ImmutableModule__set_sub_module('foo', 'bar')",
        "import tosca; del tosca.nodes.Root().__class__._name"
        "import tosca; tosca.nodes.Root().__class__.trick = 'tricky'",
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
        """import unfurl; unfurl.artifacts""",
        """from unfurl import artifacts""",
        """from unfurl.tosca_plugins import k8s; k8s.kube_artifacts""",
        "import unfurl.tosca_plugins.expr; unfurl.tosca_plugins.expr.abspath",
        "import unfurl.tosca_plugins; from unfurl.tosca_plugins import expr; expr.abspath",
        "import os.path; os.path.dirname('foo/bar')",
        "from os.path import dirname; dirname('foo/bar')",
        "from os import path; path.dirname('foo/bar')",
        """import unfurl.configurators.templates.dns; unfurl.configurators.templates.dns.unfurl_relationships_DNSRecords""",
        """import tosca
node = tosca.nodes.Root()
node._name = "test"
node.__class__.feature
        """,
        "for key, value in {'a': 1}.items(): assert key == 'a' and value == 1",
        "a, b = 1, 2; assert a == 1 and b == 2",
        "(foo := 1)",
        "try: a='ok'\nexcept: a='fail'"
    ]
    for src in allowed:
        # print("\nallowed?\n", src)
        assert _to_yaml(src, True)


@pytest.mark.skipif(not os.getenv("UNFURL_TMPDIR"), reason="UNFURL_TMPDIR not set")
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


dsl_definitions_yaml = """
dsl_definitions:
  def1: &def1
    SomeYaml: "yes"
  def2: &def2
    MoreYaml: yes

topology_template:
  node_templates: {}
  relationship_templates: {}
"""


def test_dsl_definitions():
    python_src, parsed_yaml = _to_python(dsl_definitions_yaml)
    # print(python_src)
    # pprint(parsed_yaml)
    parsed_yaml.pop("topology_template")
    test_yaml = _to_yaml(python_src, True)
    test_yaml.pop("topology_template")
    # yaml.dump(test_yaml, sys.stdout)
    assert parsed_yaml == test_yaml


def test_typeinfo():
    t = tosca.pytype_to_tosca_type(Optional[Dict[str, Dict[str, typing.Any]]])
    assert t.types == (Dict[str, typing.Any],)
    assert t.simple_types == (dict,)
    assert t.instance_check({"D": {"a": {}}})
    assert not t.instance_check({"D": []})

    t2 = tosca.pytype_to_tosca_type(Dict[str, typing.List[str]])
    assert t2.simple_types == (list,)
    assert t2.instance_check({"not default": ["test"]})
    assert not t2.instance_check({"not default": ""})


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = sys.argv[1]
    else:
        target = ""
    if not target or target == "tosca":
        _generate_builtin(
            yaml2python.generate_builtins, "tosca-package/tosca/builtin_types"
        )
        if target:
            sys.exit(0)
    if not target or target == "unfurl":
        _generate_builtin(
            yaml2python.generate_builtin_extensions, "unfurl/tosca_plugins/tosca_ext"
        )
        if target:
            sys.exit(0)
    # regenerate template modules:
    yaml2python.Convert.convert_built_in = True
    path = os.path.abspath(os.path.dirname(unfurl.__file__))
    yaml_src = f"""
    repositories:
      unfurl:
        url: file:{path}
    imports:
    """
    if not target:
        yaml_src += """
          - repository: unfurl
            file: configurators/templates/helm.yaml
          - repository: unfurl
            file: configurators/templates/dns.yaml
          - repository: unfurl
            file: configurators/templates/docker.yaml
          - repository: unfurl
            file: configurators/templates/supervisor.yaml
          - repository: unfurl
            file: tosca_plugins/artifacts.yaml
          - repository: unfurl
            file: tosca_plugins/k8s.yaml
          - repository: unfurl
            file: tosca_plugins/googlecloud.yaml
          - repository: unfurl
            file: tosca_plugins/localhost.yaml
        """
    else:
        yaml_src += f"""
          - repository: unfurl
            file: {target}
        """
    manifest = Manifest(path)
    _to_python(yaml_src, 7, tosca.WritePolicy.always, manifest)
