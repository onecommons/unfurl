# TOSCA Python DSL

A Python representation of TOSCA 1.3. This package converts TOSCA YAML to Python and vice versa. It can be used as a library.

## Why?

IDE integration
Expressiveness
Reduced learning curve

### Why Python?

Python is a simple language that is widely 
Gradual typing
Ease of integration, for example, Pydantic

## Examples

Python representation of the example in section 9.3.4.2 of the TOSCA 1.3 Specification

```python
import tosca
class WordPress(tosca.nodes.WebApplication):
    admin_user: str
    admin_password: str
    db_host: str

    database_endpoint: "tosca.relationships.ConnectsTo | tosca.nodes.Database | tosca.capabilities.EndpointDatabase"
```

This will be converted to:

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

## Safe mode

To enable untrusted Python services templates to be safely parsed in the same contexts as TOSCA YAML files, the `python_to_yaml` function has a `safe_mode` flag that will execute the Python code in a sandboxed environment. The following rules apply to code running in the sandbox:

* Only access a safe subset of Python built-ins functions and objects that do not perform IO or modify global state.
* Imports are limited to relative imports, TOSCA repositories via the  `tosca_repository` package, or the modules named in the `tosca.python2yaml.ALLOWED_MODULES` list, which defaults to "tosca", "typing", "typing_extensions", "random", "math", "string", "DateTime", and "unfurl".
* If a modules in the `ALLOWED_MODULES` has a `__safe__` attribute that is a list of names, only those attributes can be accessed by the sandboxed code. Otherwise only attributes listed in `__all__` can be access.
* Modules in the `ALLOWED_MODULES` can not be modified, nor can objects, functions or classes declared in the module (this is enforced by checking the object `__module__` attribute).
* All other modules imported have their contents executed in the same sandbox.
* Disallowed imports will only raise `ImportError` when an imported attribute is accessed.
* In safe mode, `python_to_yaml` will not invoke Python methods when convert operations to YAML. Since `ImportError`s are deferred until the imported module is accessed, this allows safe mode to parse Python code with unsafe imports in global scope if they aren't accessed while declaring types and templates.
