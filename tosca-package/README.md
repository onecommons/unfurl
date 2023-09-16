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
from tosca.python2yaml import convert_to_tosca
import yaml
import sys

with open(src_path) as f:
    python_src = f.read()
tosca_template = python_to_yaml(python_src)
yaml.dump(tosca_template, sys.stdout)
```

## Safe mode

