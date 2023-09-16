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

ensemble_with_root = (
    ensemble
    + """
        the_app:
          type: NestedApp
          directives: []
          properties: {}
          requirements:
          - nested:
              node: nested1

      root:
        node: the_app

    node_types:
      NestedApp:
        derived_from: tosca.nodes.Root
        requirements:
          - nested:
              node: Nested

"""
)

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
nested_type_import = (
    nested_import_types
    + """
topology_template:
  substitution_mappings:
    node_type: Nested
"""
)

nested_node_import = (
    nested_import_types
    + """
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
)


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
        assert (
            "outer"
            == manifest2.rootResource.find_instance("nested1").attributes[
                "nested_attribute"
            ]
        )
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

        # test serialization and loading:
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
        assert (
            "outer"
            == manifest2.rootResource.find_instance("nested1").attributes[
                "nested_attribute"
            ]
        )
        jsonExport = to_deployment(manifest2.localEnv)
        assert jsonExport["ResourceType"]["Nested"]["directives"] == ["substitute"]
        assert list(jsonExport["Resource"]) == [
            "external",
            "nested1",
            "nested1:inner",
            "nested2",
            "nested2:inner",
        ]


nested_with_placeholder_import = """
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

  NestedWithPlaceHolder:
    derived_from: tosca:Root
    requirements:
    - placeholder:
        node: placeholder
    interfaces:
      Standard:
       operations:
        create:
          implementation: Template
          inputs:
            done:
              status: ok

topology_template:
  substitution_mappings:
    node_type: NestedWithPlaceHolder

  node_templates:
    placeholder:
      type: nodes.Test
      directives:
      - default

    inner:
      type: nodes.Test
      requirements:
        - host: placeholder
"""

replaceholder_ensemble = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
spec:
  service_template:
    imports:
      - file: ./nested1.yaml
    topology_template:
      node_templates:
        replacement:
          type: nodes.Test
          # XXX plan should create this instance despite this directive
          # directives:
          # - dependent

        nested1:
          type: NestedWithPlaceHolder
          directives:
          - substitute
          requirements:
            - placeholder: replacement
"""


def test_replace_placeholder():
    cli_runner = CliRunner()
    with cli_runner.isolated_filesystem():
        init_project(
            cli_runner,
            args=["init", "--mono", "--skeleton=aws"],
            env=dict(UNFURL_HOME=""),
        )
        with open("ensemble-template.yaml", "w") as f:
            f.write(replaceholder_ensemble)

        with open("nested1.yaml", "w") as f:
            f.write(nested_with_placeholder_import)

        manifest = YamlManifest(localEnv=LocalEnv(".", homePath="./unfurl_home"))
        node1 = manifest.tosca.get_template("nested1")
        assert node1 and node1.substitution and node1.topology is not node1.substitution
        inner_nodespec = node1.substitution.get_node_template("inner")
        assert inner_nodespec
        assert (
            inner_nodespec.requirements["host"].relationship.target.name
            == "replacement"
        )
        # verify inner matches with external not placeholder

        result, job, summary = run_job_cmd(cli_runner, print_result=True)
        inner = job.rootResource.find_instance("nested1:inner")
        assert inner
        assert inner.query(".targets::host::.name") == "replacement"
        replacement = job.rootResource.find_instance("replacement")
        assert replacement
        assert replacement.query(".sources::host::.name") == "inner"

        assert summary["job"] == {
            "id": "A01110000000",
            "status": "ok",
            "total": 2,
            "ok": 2,
            "error": 0,
            "unknown": 0,
            "skipped": 0,
            "changed": 2,
        }

        # # test serialization and loading:
        manifest2 = YamlManifest(localEnv=LocalEnv(".", homePath="./unfurl_home"))
        inner = manifest2.rootResource.find_instance("nested1:inner")
        assert inner
        assert inner.query(".targets::host::.name") == "replacement"
        replacement = manifest2.rootResource.find_instance("replacement")
        assert replacement
        assert replacement.query(".sources::host::.name") == "inner"


def test_substitution_plan():
    cli_runner = CliRunner()
    with cli_runner.isolated_filesystem():
        init_project(
            cli_runner,
            args=["init", "--mono", "--skeleton=aws"],
            env=dict(UNFURL_HOME=""),
        )
        with open("ensemble-template.yaml", "w") as f:
            f.write(ensemble_with_root)

        with open("nested1.yaml", "w") as f:
            f.write(nested_node_import)

        manifest = YamlManifest(localEnv=LocalEnv(".", homePath="./unfurl_home"))
        node1 = manifest.tosca.get_template("nested1")
        assert node1 and node1.substitution and node1.topology is not node1.substitution
        inner_nodespec = node1.substitution.get_node_template("inner")
        assert inner_nodespec
        result, job, summary = run_job_cmd(cli_runner, print_result=True)
        assert job.json_summary()["job"] == {
            "id": "A01110000000",
            "status": "ok",
            "total": 3,
            "ok": 3,
            "error": 0,
            "unknown": 0,
            "skipped": 0,
            "changed": 3,
        }


attribute_access_ensemble = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
spec:
  service_template:
    imports:
      - file: ./nested1.yaml

    topology_template:
      node_templates:
        nested1:
          type: NestedApp
          directives:
          - substitute
          requirements:
            - db: external
            # replace dockerhost in nested topology:
            - dockerhost: containerhost

        containerhost:
          type: ComputeContainerHost

        external:
          type: DBHost
          interfaces:
            Standard:
              operations:
                create:
                  implementation: Template
                  inputs:
                    resultTemplate:
                      attributes:
                        ip_address: 10.10.10.1
                    done:
                      status: ok
      outputs:
        db_host:
          value:
            eval: ::containerhost::user_data::DB_HOST
"""

attribute_access_import = """
node_types:
  DBHost:
    derived_from: tosca.nodes.Root
    attributes:
      ip_address:
        type: string

  ContainerHost:
    derived_from: tosca.nodes.Root

  Container:
    derived_from: tosca.nodes.Root
    properties:
      env:
        type: map
    requirements:
    - host:
        relationship: unfurl.relationships.ConfiguringHostedOn
        node: ContainerHost

  ComputeContainerHost:
    derived_from: ContainerHost
    attributes:
      container_env:
        type: map
        required: false
        default:
          eval: .sources::host::env
    interfaces:
      Standard:
        operations:
          create:
            implementation: Template
            inputs:
              done:
                status: ok
              resultTemplate:
                attributes:
                  user_data: "{{ SELF.container_env }}"

  NestedApp:
    derived_from: tosca.nodes.Root
    requirements:
      - db:
          relationship: unfurl.relationships.Configures
          node: DBHost 
      - container: 
          relationship: unfurl.relationships.Configures
          node: container

topology_template:
  node_templates:
    nested:
      type: NestedApp

    dockerhost:
      type: ContainerHost

    container:
      type: Container
      properties:
        env:
          eval:
            to_env:
              DB_HOST:
                eval:
                  .configured_by::.targets::db::ip_address
      requirements:
        - host:
            node: dockerhost

  substitution_mappings:
    node: nested
"""


def test_attribute_access():
    cli_runner = CliRunner()
    with cli_runner.isolated_filesystem():
        init_project(
            cli_runner,
            args=["init", "--mono", "--skeleton=aws"],
            env=dict(UNFURL_HOME=""),
        )
        with open("ensemble-template.yaml", "w") as f:
            f.write(attribute_access_ensemble)

        with open("nested1.yaml", "w") as f:
            f.write(attribute_access_import)

        manifest = YamlManifest(localEnv=LocalEnv(".", homePath="./unfurl_home"))
        result, job, summary = run_job_cmd(cli_runner, print_result=True)
        assert job.get_outputs() == {"db_host": "10.10.10.1"}
        assert job.json_summary()["job"] == {
            "id": "A01110000000",
            "status": "ok",
            "total": 2,
            "ok": 2,
            "error": 0,
            "unknown": 0,
            "skipped": 0,
            "changed": 2,
        }
