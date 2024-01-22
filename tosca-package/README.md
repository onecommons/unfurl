# A TOSCA Python-based DSL

This package implements a Python representation of [TOSCA 1.3](https://docs.oasis-open.org/tosca/TOSCA-Simple-Profile-YAML/v1.3/os/TOSCA-Simple-Profile-YAML-v1.3-os.html). It converts TOSCA YAML to Python and vice versa. It can be used as a library by a TOSCA processor or as a stand-alone conversion tool.

TOSCA (Topology and Orchestration Specification for Cloud Applications) is an [OASIS open standard](https://www.oasis-open.org/committees/tc_home.php?wg_abbrev=tosca) that provides language to describe a topology of cloud based web services, their components, relationships, and the processes that manage them. TOSCA provides mechanisms for abstraction and composition, thereby enabling portability and automated management across cloud providers regardless of underlying platform or infrastructure.

## Why a DSL (Domain Specic Language)?

Why build a DSL for TOSCA? Or more precisely, why build an [embedded (or internal) DSL](https://en.wikipedia.org/wiki/Domain-specific_language#External_and_Embedded_Domain_Specific_Languages) -- a DSL that is a subset of existing programming language?

* Avoid what is affectionately known as "YAML hell". YAML's syntax is limited to expressing basic data types and has [various quirks](https://noyaml.com) that makes it hard to manage at scale. But TOSCA is a fairly strongly typed language, so expressing it in a syntax that can reflect that makes for a much more powerful solution with regard to developer usability and tooling.

* Expressiveness. With a full-fledged programming language as the host language (ie. Python), you have all its facilities for writing good code.

* Reduced learning curve. By mapping to TOSCA syntax to existing language constructs like classes and methods and by rely on type inference we can present a simpler and more intuitive mental model to developers. For example, TOSCA's notions of requirements, capabilities, properties, and artifacts are all represented the same way as regular Python attributes assigned to a Python class.

* IDE and tooling integration. You can take advantage of all of the existing IDE and tooling integrations for the host language (ie. Python).

![VS Code displaying tooltip](https://github.com/onecommons/unfurl/raw/main/tosca-package/vs-tosca-python-error-tooltip.png)

Using our TOSCA DSL, VS-Code will detect missing or invalid node template properties and requirements out of the box.

### Why Python?

* Accessability. Python is an easy-to-use language usable by all skill levels of developers and the second mostly widely known programming language (after Javascript).
* Ease of integration. There is a huge ecosystem of Python tools and library in the cloud computing and DevOps space and using a Python DSL makes it easy to integrate them. For example, we plan to integrate [Pydantic](https://pydantic.dev/) for data validation of properties and attributes.
* Gradual typing. Type annotations are optional in Python allowing for rapid prototyping and making it easy to apply TOSCA to wide range of use cases. You can quickly build loosely defined models for on-off sys admin tasks or complex models with statically enforced constraints using Python's meta-programming facilities.

## Installation

If you want to use this as a Python library, `pip install tosca`. For a command line tool that converts between YAML and Python, or for a TOSCA orchestrator with native support, install [unfurl](https://github.com/onecommons/unfurl), which integrates this library.

Requirements: Python versions 3.7 or later.

## Status

Experimental, syntax subject to change based on feedback. Currently converts a significant subset of TOSCA 1.3, to and from Python and YAML but there are still significant gaps. 100% coverage is not a goal since YAML can be used as a fallback for the less common elements. Also, YAML-to-Python completeness is less of a priority than Python-to-YAML since the former is a just a convenience, while the latter is required for the DSL to be useable.

## Examples

Let's start with the “hello world” template in the TOSCA 1.3 Specification (Section 2.1, Example 1):

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
from tosca import *  # imports GB and MB scalars
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

It looks very similar, except explicit types are required when assigning the `host` and `os` capabilities and properties that have unit values (`disk_size` and `mem_size`).

Things are a little more interesting when defining a TOSCA node type in Python.
Consider this example from the TOSCA 1.3 Specification (in section 9.3.4.2):

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

Here we see how our DSL can infer whether a field is a TOSCA property or TOSCA requirement based on the field's type -- for example, only requirements can be assigned a relationship, so `database_endpoint` must be a TOSCA requirement, and data types like strings default to TOSCA properties.

If you need to specify TOSCA specific information about the field or need to resolve ambiguity about the fields type, you use can field specifiers. For example, consider this Python representation of the `tosca.nodes.Compute` node type defined in the TOSCA 1.3 spec:

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
        "capabilities.Attachment"
    ] = Requirement(default=())
```

Here the `Attribute()` field specifier is used to indicate a field is an TOSCA attribute, not a property (the default for data types), and `Capability()` and `Requirement()` also used as field specifiers. Note that we can infer that `local_storage` has `occurrences: [0, UNBOUNDED]` because the type is a sequence and its default value is an empty sequence. See the [API documentation](https://docs.unfurl.run/api.html#tosca-field-specifiers) for the full list of field specifiers.

Also note `_type_name`, which can be used to name the type when the YAML identifier doesn't conform to Python's identifier syntax.

You can see all of TOSCA 1.3's pre-defined types as automatically converted from YAML to Python [here](https://github.com/onecommons/unfurl/blob/main/tosca-package/tosca/builtin_types.py).

### Interfaces and Operations

By mapping TOSCA's interfaces and operations to Python's classes and methods we can create a TOSCA syntax that is concise, intuitive, and easily statically validated. Consider this definition of a custom interface and its implementation by a node type:

```python
  class MyCustomInterface(tosca.interfaces.Root):
      class Inputs(ToscaInputs):
          location: str
          version: int = 0

      def my_operation(self):
          "description of my_operation"
          ...  # an abstract operation, subclass needs to implement 

  class Example(tosca.nodes.Root, MyCustomInterface):
      shellScript = tosca.artifacts.ImplementationBash(file="example.sh")
      location: str
      host: tosca.nodes.Compute

      def my_operation(self):
          return self.shellScript.execute(
              MyCustomInterface.Inputs(location=self.location),
              host_address=self.host.public_address
          )
```

This will be translated to YAML as:

```yaml
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

node_types:
    Example:
        derived_from: tosca.nodes.Root
        artifacts:
          shellScript:
            type: tosca.artifacts.ImplementationBash
            file: example.sh
        properties:
          location:
            type: string
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
                    {get_property: [SELF, location]}
                  host_address:
                    {get_property: [SELF, host, public_address]}
```

It interesting to note that the readability improvements in this example stem not just from concision (18 lines vs. 37 lines) but also because of the syntax highlighting for Python -- something most Markdown processors support while none support that for TOSCA -- another illustration of the benefits of building a DSL on a widely supported language.

### Orchestrator Integration

As the example above shows, operations don't actually perform the work -- instead they return the implementation (e.g. artifacts and inputs) that the orchestrator will execute. That's about as much as you could expect from a pure Python-to-YAML converter.

But if the DSL is integrated into the orchestrator's runtime, the operation's implementation can be defined in the class definition too. For example:

```python
import tosca

class Integrated(tosca.nodes.Root):
    hostname: str

    def _url(self) -> str:
        return f"https://{self.hostname}/"

    url: str = tosca.Computed(factory=_url)

    def run(self, task):
        # invoked by the orchestrator when the operation is executed
        do_stuff(self.url)
        return True

    def create(self):
        return self.run
```

Here the `create()` operation returns the `run()` method, which will be invoked by the orchestrator. This example also illustrates another mechanism for integrating with the orchestrator by using the `Computed()` field specifier to declare a TOSCA property whose value is computed by the decorated method at runtime.

Unfurl provides this integration and other orchestrators can implement a callback interface to provide similar integration.  In addition, Unfurl will automatically run operation methods during the Unfurl's planning and render phase if the method executes runtime-only functionality that isn't available when generating YAML.

### Node Filters

Tosca types can declared a special class-level method called `_class_init` that is called when the class definition is being initialized. Inside this method, expressions that reference fields return references to the field definition, not its value, allowing you to customize the class definition in a context where type checker (include the IDE) has the class definition available.

This example creates sets the `node_filter` on the declared `host` requirement:


```python
    from tosca import in_range, gb

    class Example(tosca.nodes.Root):
        host: tosca.nodes.Compute

        @classmethod
        def _class_init(cls) -> None:
            in_range(2 * gb, 20 * gb).apply_constraint(cls.host.host.mem_size)
```

And will be translated to YAML as:

```yaml
node_types:
  Example:
    derived_from: tosca.nodes.Root
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
```

Note that your IDE's type checker will detect if the `mem_size`'s type was incompatible with the values passed to `in_range`.

### Names and identifiers

TOSCA's YAML syntax allows names that are not valid Python identifiers so the DSL's APIs let you provide an alternative name for types, templates, fields, and operations, etc. which will used when generating YAML -- whose those if the TOSCA name is not a valid [Python identifier](https://docs.python.org/3/reference/lexical_analysis.html#identifiers) or if the name starts with a `_`.  Names that starts with `_` are reserved for internal use by your DSL classes and are ignored when generating YAML. When converting YAML to Python the code generator will generate Python code that preserves non-conforming names as needed.

### Imports and repositories

We translate TOSCA imports statements as relative imports in Python or, if a repository was specified, as a Python import in a package named "tosca_repositories.<repository_name>". For example:

```yaml
  imports:
    - file: foo.yaml
    - file: foo.yaml
      namespace_prefix: ns
    - file: foo.yaml
      namespace_prefix: foo
    - file: ../bar/foo.yaml
    - file: foo.yaml
      repository: my_repo
    - file: bar/foo.yaml
      repository: my_repo
    - file: bar/foo.yaml
      repository: my_repo
      namespace_prefix: foo
```

will be translated to:

```python
from .foo import *
from . import foo as ns
from . import foo
from ..bar.foo import *
from tosca_repositories.my_repo.bar.foo import *
from tosca_repositories.my_repo.bar import foo
```

`unfurl export` will resolve imports from a repository by creating a`tosca_repositories` directory with a symlink to the location of the repository. This enables compatibility with IDEs that rely on simple file system path mapping to resolve Python imports.

## Usage

### YAML to Python

This example converts the "service_template.yaml" to Python and save the result at "service_template.py":

```python
from tosca.yaml2python import yaml_to_python

# yaml_to_python() converts the given YAML service template to Python source code as a string and saves it to a file if a second file path is provided.
python_src = yaml_to_python("service_template.yaml", "service_template.py")
```

If you are using unfurl, you can accomplish the same thing from the command line using:

`unfurl export --format python service_template.yaml`

The following options affect the output of the generated Python code:

```
  --python-target [3.7|3.8|3.9|3.10]
                                  Python version to target (Default: current version)
  --overwrite [older|never|always|auto]
                                  Overwrite existing files (Default: auto)
```

The conversion process will follow TOSCA imports and generate Python files alongside the imported YAML files. The `--overwrite` controls what happens when a file with the same name already exists; its values can be:

* `older` will only overwrite the output file if it is older than the source file.
* `never` will never overwrite an existing file.
* `always` will always write the file even if one currently exists.
* `auto` (the default) will check if the existing file contains a generated comment at the beginning of the file. If The header comment is missing or the modified time included in the comment does not match the file's modified time, the file will be skipped. This way files with manually modifications won't be overwritten. To always allow a file to overwritten, edit the header to include the string "overwrite ok".

### Python to YAML

This example does the reverse, saving the Python file as a YAML file:

```python
tosca_template = python_to_yaml("service_template.py", "service_template.yaml", safe_mode=False)
```

With unfurl, the command line equivalent is:

`unfurl export service_template.py`

## Safe Mode

To enable untrusted Python service templates to be safely parsed in the same contexts as TOSCA YAML files, the `python_to_yaml` function has a `safe_mode` flag that will execute the Python code in a sandboxed environment. The following rules apply to code running in the sandbox:

* The sandbox only a provides a subset of Python's built-ins functions and objects -- ones that do not perform IO or modify global state.
* Imports are limited to relative imports, TOSCA repositories via the  `tosca_repository` package, or the modules named in the `tosca.python2yaml.ALLOWED_MODULES` list, which defaults to "tosca", "typing", "typing_extensions", "random", "math", "string", "DateTime", and "unfurl".
* If a module in the `ALLOWED_MODULES` has a `__safe__` attribute that is a list of names, only those attributes can be accessed by the sandboxed code. Otherwise only attributes listed in `__all__` can be accessed.
* Modules in the `ALLOWED_MODULES` can not be modified, nor can objects, functions or classes declared in the module (this is enforced by checking the object `__module__` attribute).
* All other modules imported have their contents executed in the same sandbox.
* Disallowed imports will only raise `ImportError` when an imported module's attribute is accessed.
* In safe mode, `python_to_yaml` will not invoke Python methods when convert operations to YAML. Since `ImportError`s are deferred until the imported module's attributes are accessed, this allows safe mode to parse Python code with unsafe imports in global scope as long as they aren't accessed while declaring types and templates in global scope.

## API Documentation

API Documentation can be found [here](https://docs.unfurl.run/api.html#api-for-writing-service-templates).