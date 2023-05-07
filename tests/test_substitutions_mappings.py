from click.testing import CliRunner

from unfurl.job import JobOptions, Runner
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

nested1_import = """
node_types:
  nodes.Test:
    derived_from: tosca.nodes.Root
    interfaces:
      Standard:
       operations:
        create:
          implementation: Template
          inputs:
            # XXX: resultTemplate:
            #   attributes:
            #     foo:
            #       eval: .targets::host::foo
            done:
              status: ok
  Nested:
    derived_from: nodes.Test
    requirements:
      - host:
          relationship: unfurl.relationships.ConfiguringHostedOn

topology_template:
  substitution_mappings:
    node_type: Nested
  # node_templates:
    # nested:
    #   type: Nested
    #   requirements:
    #     inner: inner
    # inner:
    #   type: inner
    #   properties:
    #     from_outer:
    #       eval:
    #         ::aws::outer_prop
"""


def test_substitution():
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
            f.write(nested1_import)

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
