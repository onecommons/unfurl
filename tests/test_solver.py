import sys
from unfurl.yamlloader import load_yaml, yaml
from unfurl.solver import solve_topology, tosca_to_rust, Node, solve, Field, FieldValue, ToscaValue, SimpleValue
from toscaparser.tosca_template import ToscaTemplate
from toscaparser.properties import Property
from toscaparser.elements.portspectype import PortSpec
from ruamel.yaml.comments import CommentedMap

def make_tpl(yaml_str: str):
    tosca_yaml = load_yaml(
        yaml, yaml_str, readonly=True
    )  # export uses readonly yaml parser
    tosca_yaml["tosca_definitions_version"] = "tosca_simple_unfurl_1_0_0"
    if "topology_template" not in tosca_yaml:
        tosca_yaml["topology_template"] = dict(
            node_templates={}, relationship_templates={}
        )
    return ToscaTemplate(path=__file__, yaml_dict_tpl=tosca_yaml)

example_helloworld_yaml = """
description: Template for deploying a single server with predefined properties.
node_types:
  Example:
    derived_from: tosca.nodes.Root
    properties:
      prop1:
        type: string
    requirements:
    - host:
        capability: tosca.capabilities.Compute
        node: tosca.nodes.Compute

topology_template:
  substitution_mappings:
    node: db_server

  node_templates:
    app:
      type: Example
      properties:
        prop1: example
      requirements:
      - host:
          node_filter:
            capabilities:
              - os:
                  properties:
                    - type: linux

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

def test_convert():
    for val, toscatype in [
        (80, "PortDef"),
        (CommentedMap(), "map"),
        (PortSpec.make("80:80"), "tosca.datatypes.network.PortSpec"),
        ]:
        prop = Property(
            toscatype,
            val,
            dict(type=toscatype),
        )
        assert tosca_to_rust(prop)

def test_solve():
    f = Field('f', FieldValue.Property(ToscaValue(SimpleValue.integer(0))))
    na = Node("a", "Foo", fields=[f])
    assert na.name == "a"
    assert na.tosca_type == "Foo"
    assert na.fields == [f]
    nodes = {"a": Node("a"), "b": Node("b")}
    types = dict(a=["a", "Root"])
    solved = solve(nodes, types)
    assert not solved
    
    tosca = make_tpl(example_helloworld_yaml)
    assert tosca.topology_template

    solved = solve_topology(tosca.topology_template)

    assert solved == {("app", "host"): [("db_server", "host")]}
    app = tosca.topology_template.node_templates["app"]
    assert not app.missing_requirements
    for rel_template, original_tpl, requires_tpl_dict in app.relationships:
        assert (
            rel_template.target == tosca.topology_template.node_templates["db_server"]
        )

    # XXX
    # test requirement match for each type of CriteriaTerm and Constraint
    # test restrictions


def test_node_filter():
    tosca_tpl = (
        os.path.dirname(__file__)
        + "/../tosca-parser/samples/tests/data/node_filter/test_node_filter.yaml"
    )
    t = ToscaTemplate(tosca_tpl, import_resolver=ImportResolver(None))
    filter_match = (
        t.topology_template.node_templates["test"].relationships[0][0].target.name
    )
    assert filter_match == "server_large", filter_match

    # delete match
    del t.tpl["topology_template"]["node_templates"]["test"]["requirements"][0]["host"][
        "node"
    ]
    # add an unsupported pattern, match should be skipped
    t.tpl["topology_template"]["node_templates"]["test"]["requirements"][0]["host"][
        "node_filter"
    ]["capabilities"][0]["host"]["properties"].append(
        {"distribution": {"pattern": "u*"}}
    )

    t2 = ToscaTemplate(yaml_dict_tpl=t.tpl, import_resolver=ImportResolver(None))
    assert not t2.topology_template.node_templates["test"].relationships
