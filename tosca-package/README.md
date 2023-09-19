# TOSCA Python DSL

A Python representation of TOSCA 1.3. This package converts TOSCA YAML to Python and vice versa. It can be used as a library.

## Why?

IDE integration
Avoid "YAML hell"
Expressiveness
Reduced learning curve

### Why Python?

Python is a simple language that is widely used
Ease of integration, for example, Pydantic
Gradual typing

## Install

Python versions 3.7 and later are supported.

## Status


## Examples

Let's start with the “hello world” template in the TOSCA 1.3 Specificatin (Section 2.1, Example 1):

```yaml
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
```

Here's the same template translated to Python:

```python
"""Template for deploying a single server with predefined properties."""
import tosca
from tosca import *  # imports GB, MB scalars
db_server = tosca.nodes.Compute(
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
```

It is very similar, except explicit types are required for assigning the capabilities and unit scalars.

Things are a little more interesting when defining TOSCA node types in Python.
Consider this example of a node type from the TOSCA 1.3 Specification (in section 9.3.4.2 ):

```yaml
node_types:
  WordPress:
    derived_from: tosca.nodes.WebApplication
    properties:
        admin_user:
          type: string
        admin_password:
          type: string
        db_host:
          type: string
    requirements:
      - database_endpoint:
          capability: tosca.capabilities.Endpoint.Database
          node: tosca.nodes.Database
          relationship: tosca.relationships.ConnectsTo
```

Its Python representation looks like:

```python
import tosca
class WordPress(tosca.nodes.WebApplication):
    admin_user: str
    admin_password: str
    db_host: str

    database_endpoint: tosca.relationships.ConnectsTo | tosca.nodes.Database | tosca.capabilities.EndpointDatabase
```

Here type declaration can infer whether a field is a TOSCA property or requirement based on the field's type -- only requirements can be assigned a relationship, so `database_endpoint` must be a TOSCA requirement, and data types like strings default as TOSCA properties.

If you need to specify TOSCA specific information about the field or need to resolve ambiguity about the fields type, you use can field specifiers. For example, consider the Python representation of the `tosca.nodes.Compute` node type defined in the TOSCA 1.3 spec: 

```python
class Compute(AbstractCompute):
    _type_name = "tosca.nodes.Compute"
    private_address: str = Attribute()
    public_address: str = Attribute()
    networks: Dict[str, "datatypes.NetworkNetworkInfo"] = Attribute()
    ports: Dict[str, "datatypes.NetworkPortInfo"] = Attribute()

    host: "capabilities.Compute" = Capability(
        factory=capabilities.Compute,
        valid_source_types=["tosca.nodes.SoftwareComponent"],
    )

    binding: "capabilities.NetworkBindable" = Capability(
        factory=capabilities.NetworkBindable
    )

    os: "capabilities.OperatingSystem" = Capability(
        factory=capabilities.OperatingSystem
    )

    scalable: "capabilities.Scalable" = Capability(factory=capabilities.Scalable)

    endpoint: "capabilities.EndpointAdmin" = Capability(
        factory=capabilities.EndpointAdmin
    )

    local_storage: Sequence[
            "relationships.AttachesTo" |
            "nodes.StorageBlockStorage" |
            "capabilities.Attachment" |
        ] = Requirement(default=())
```

Here we see the `Attribute()` field specifier being used to indicate a field is an TOSCA attribute not a property and `Capability()` and `Requirement()` also used as field specifiers. Note that we can infer that `local_storage` has `occurrences = [0, UNBOUNDED]` because the type is a sequence and its default value is an empty sequence.

Also note `_type_name`, which can be used when a YAML TOSCA identifier doesn't conform to a valid Python identifier.

You can see all of TOSCA 1.3 built in types as automatically converted to Python [here](https://github.com/onecommons/unfurl/blob/dsl/tosca-package/tosca/builtin_types.py).

## Usage
### YAML to Python

```python
from tosca.yaml2python import yaml_to_python

# yaml_to_python() converts the given YAML service template to Python source code as a string and saves it to a file if a second file path is provided.
python_src = yaml_to_python("service_template.yaml", "service_template.py")
```

### Python to YAML

```python
from tosca.python2yaml import python_to_yaml
import yaml
import sys

with open(src_path) as f:
    python_src = f.read()
tosca_template = python_to_yaml(python_src, safe_mode=False)
yaml.dump(tosca_template, sys.stdout)
```

## Safe Mode

To enable untrusted Python services templates to be safely parsed in the same contexts as TOSCA YAML files, the `python_to_yaml` function has a `safe_mode` flag that will execute the Python code in a sandboxed environment. The following rules apply to code running in the sandbox:

* The sandbox only a provides a subset of Python's built-ins functions and objects -- ones that do not perform IO or modify global state.
* Imports are limited to relative imports, TOSCA repositories via the  `tosca_repository` package, or the modules named in the `tosca.python2yaml.ALLOWED_MODULES` list, which defaults to "tosca", "typing", "typing_extensions", "random", "math", "string", "DateTime", and "unfurl".
* If a modules in the `ALLOWED_MODULES` has a `__safe__` attribute that is a list of names, only those attributes can be accessed by the sandboxed code. Otherwise only attributes listed in `__all__` can be accessed.
* Modules in the `ALLOWED_MODULES` can not be modified, nor can objects, functions or classes declared in the module (this is enforced by checking the object `__module__` attribute).
* All other modules imported have their contents executed in the same sandbox.
* Disallowed imports will only raise `ImportError` when an imported attribute is accessed.
* In safe mode, `python_to_yaml` will not invoke Python methods when convert operations to YAML. Since `ImportError`s are deferred until the imported module is accessed, this allows safe mode to parse Python code with unsafe imports in global scope if they aren't accessed while declaring types and templates.
