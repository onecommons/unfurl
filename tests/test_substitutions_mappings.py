import json
from click.testing import CliRunner

from unfurl.job import JobOptions, Runner
from unfurl.to_json import to_deployment
from unfurl.yamlmanifest import YamlManifest
from unfurl.localenv import LocalEnv
from .utils import init_project, run_job_cmd

#### Behavior notes ###
## properties on the substituted template in the outer topology set/override the inner template's properties
## substitution property mappings (the nested topology's inputs) of only apply to the synthesized _substitution_mapping template
## validation happens on the inner template, not the outer template
## plan creates TopologyInstance and sets shadow if node has substitute directive
## note: Plan.create_shadow_instance() sets "imported"
# serialization save nested topologies inside a "substitution" section of an instance, e.g.:
# foo:
#   type: blah
#   properties: ...
#   substitution:
#     inputs:
#     outputs:
#     instances:
#      omit children that shadow outer template

ensemble = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
spec:
  service_template:
    imports:
      - file: ./nested1.yaml
    topology_template:
      node_templates:
        external:
          type: nodes.Test
          properties:
            outer: outer

        nested1:
          type: Nested
          directives:
          - substitute
          properties:
            foo: bar
          requirements:
            - host: external

        # creates a copy of the nested topology's templates
        nested2:
          type: Nested
          directives:
          - substitute
          properties:
            foo: baz
          requirements:
            - host: external
"""

nested_import_types = """
node_types:
  nodes.Test:
    derived_from: tosca.nodes.Root
    interfaces:
      Standard:
       operations:
        create:
          implementation: Template
          inputs:
            done:
              status: ok

  Nested:
    derived_from: tosca.nodes.Root
    properties:
      foo:
        type: string
    attributes:
      nested_attribute:
        type: string
        required: false
    requirements:
      - host:
          relationship: unfurl.relationships.ConfiguringHostedOn
    interfaces:
      Standard:
       operations:
        create:
          implementation: Template
          inputs:
            resultTemplate:
              attributes:
                nested_attribute:
                  eval: .targets::host::outer
            done:
              status: ok
"""
nested_type_import = nested_import_types + """
topology_template:
  substitution_mappings:
    node_type: Nested
"""

nested_node_import = nested_import_types + """
topology_template:
  substitution_mappings:
    node: nested
  node_templates:
    nested:
      type: Nested
      requirements:
        - dependency: inner

    inner:
      type: nodes.Test
      properties:
        from_outer: "{{ ROOT.host.outer }}"
        from_outer2:
          eval:
            ::nested::.targets::host::outer
"""


def test_substitution_with_type():
    cli_runner = CliRunner()
    with cli_runner.isolated_filesystem():
        init_project(
            cli_runner,
            args=["init", "--mono", "--skeleton=aws"],
            env=dict(UNFURL_HOME=""),
        )
        with open("ensemble-template.yaml", "w") as f:
            f.write(ensemble)

        with open("nested1.yaml", "w") as f:
            f.write(nested_type_import)

        manifest = YamlManifest(localEnv=LocalEnv(".", homePath="./unfurl_home"))
        node1 = manifest.tosca.get_template("nested1")
        assert node1
        assert node1.substitution and node1.topology is not node1.substitution
        sub_mapped_node_template = (
            node1.substitution.topology_template.substitution_mappings.sub_mapped_node_template
        )
        assert sub_mapped_node_template and sub_mapped_node_template.name == "nested1"
        assert node1.substitution.substitute_of is node1
        assert sub_mapped_node_template is node1.toscaEntityTemplate
        inner_template = (
            node1.substitution.topology_template.substitution_mappings._node_template
        )
        assert inner_template
        inner_nodespec = node1.substitution.get_node_template(inner_template.name)
        assert inner_nodespec.nested_name == "nested1:_substitution_mapping"
        assert inner_nodespec.requirements["host"].entity_tpl["host"] == "external"
        assert node1.requirements["host"].entity_tpl["host"] == "external"
        external = manifest.tosca.get_template("external")
        assert [r.source.name for r in external.relationships] == ["nested1", "nested2"]

        node2 = manifest.tosca.get_template("nested2")
        assert node2
        assert node2.substitution and node2.topology is not node2.substitution
        assert (
            node1.topology is node2.topology
            and node1.substitution is not node2.substitution
        )
        assert node2.substitution.substitute_of is node2
        sub_mapped_node_template = (
            node2.substitution.topology_template.substitution_mappings.sub_mapped_node_template
        )
        assert sub_mapped_node_template and sub_mapped_node_template.name == "nested2"
        result, job, summary = run_job_cmd(cli_runner)
        assert job.json_summary() == {
            "job": {
                "id": "A01110000000",
                "status": "ok",
                "total": 3,
                "ok": 3,
                "error": 0,
                "unknown": 0,
                "skipped": 0,
                "changed": 3,
            },
            "outputs": {},
            "tasks": [
                {
                    "status": "ok",
                    "target": "external",
                    "operation": "create",
                    "template": "external",
                    "type": "nodes.Test",
                    "targetStatus": "ok",
                    "targetState": "created",
                    "changed": True,
                    "configurator": "unfurl.configurators.TemplateConfigurator",
                    "priority": "required",
                    "reason": "add",
                },
                {
                    "status": "ok",
                    "target": "_substitution_mapping",
                    "operation": "create",
                    "template": "_substitution_mapping",
                    "type": "Nested",
                    "targetStatus": "ok",
                    "targetState": "created",
                    "changed": True,
                    "configurator": "unfurl.configurators.TemplateConfigurator",
                    "priority": "required",
                    "reason": "add",
                },
                {
                    "status": "ok",
                    "target": "_substitution_mapping",
                    "operation": "create",
                    "template": "_substitution_mapping",
                    "type": "Nested",
                    "targetStatus": "ok",
                    "targetState": "created",
                    "changed": True,
                    "configurator": "unfurl.configurators.TemplateConfigurator",
                    "priority": "required",
                    "reason": "add",
                },
            ],
        }
        # print("plan", job._json_plan_summary(True))
        # with open("ensemble/ensemble.yaml") as f:
        #     print(f.read())
        manifest2 = YamlManifest(localEnv=LocalEnv(".", homePath="./unfurl_home"))
        assert manifest2.rootResource.find_instance("nested2:_substitution_mapping")
        expected = ["root", "external", "nested1", "nested2"]
        assert expected == [
            i.name for i in manifest2.rootResource.get_self_and_descendants()
        ]
        assert "outer" == manifest2.rootResource.find_instance("nested1").attributes["nested_attribute"]
        jsonExport = to_deployment(manifest2.localEnv)
        assert jsonExport["ResourceType"]["Nested"]["directives"] == ["substitute"]
        assert list(jsonExport["Resource"]) == ["external", "nested1", "nested2"]

def test_substitution_with_node():
    cli_runner = CliRunner()
    with cli_runner.isolated_filesystem():
        init_project(
            cli_runner,
            args=["init", "--mono", "--skeleton=aws"],
            env=dict(UNFURL_HOME=""),
        )
        with open("ensemble-template.yaml", "w") as f:
            f.write(ensemble)

        with open("nested1.yaml", "w") as f:
            f.write(nested_node_import)

        manifest = YamlManifest(localEnv=LocalEnv(".", homePath="./unfurl_home"))
        node1 = manifest.tosca.get_template("nested1")
        assert node1
        assert node1.substitution and node1.topology is not node1.substitution
        sub_mapped_node_template = (
            node1.substitution.topology_template.substitution_mappings.sub_mapped_node_template
        )
        assert sub_mapped_node_template and sub_mapped_node_template.name == "nested1"
        assert node1.substitution.substitute_of is node1
        assert sub_mapped_node_template is node1.toscaEntityTemplate
        inner_template = (
            node1.substitution.topology_template.substitution_mappings._node_template
        )
        assert inner_template
        inner_nodespec = node1.substitution.get_node_template(inner_template.name)
        assert inner_nodespec.nested_name == "nested1:nested"
        assert inner_nodespec.requirements["host"].entity_tpl["host"] == "external"
        assert node1.requirements["host"].entity_tpl["host"] == "external"
        external = manifest.tosca.get_template("external")
        assert [r.source.name for r in external.relationships] == ["nested1", "nested2"]

        result, job, summary = run_job_cmd(cli_runner)
        assert job.json_summary()["job"] == {
                "id": "A01110000000",
                "status": "ok",
                "total": 5,
                "ok": 5,
                "error": 0,
                "unknown": 0,
                "skipped": 0,
                "changed": 5,
            }


        manifest2 = YamlManifest(localEnv=LocalEnv(".", homePath="./unfurl_home"))

        inner = manifest2.rootResource.find_instance("nested1:inner")
        assert inner
        assert inner.attributes["from_outer2"] == "outer"        
        assert inner.attributes["from_outer"] == "outer"

        assert manifest2.rootResource.find_instance("nested2:nested")
        expected = ["root", "external", "nested1", "nested2"]
        assert expected == [
            i.name for i in manifest2.rootResource.get_self_and_descendants()
        ]
        assert "outer" == manifest2.rootResource.find_instance("nested1").attributes["nested_attribute"]
        jsonExport = to_deployment(manifest2.localEnv)
        assert jsonExport["ResourceType"]["Nested"]["directives"] == ["substitute"]
        assert list(jsonExport["Resource"]) == ['external', 'nested1', 'nested1:inner', 'nested2', 'nested2:inner']
