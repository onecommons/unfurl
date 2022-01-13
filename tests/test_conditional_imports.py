from click.testing import CliRunner

from unfurl.job import JobOptions, Runner
from unfurl.yamlmanifest import YamlManifest
from unfurl.localenv import LocalEnv
from .utils import init_project, print_config

ensemble = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
spec:
  service_template:
    imports:
      - file: ./gcp.yaml
        when: .primary_provider[type=unfurl.relationships.ConnectsTo.GoogleCloudProject]
      - file: ./aws.yaml
        when: .primary_provider[type=unfurl.relationships.ConnectsTo.AWSAccount]
"""

aws_import = """
node_types:
  aws:
    derived_from: tosca:Root
"""

gcp_import = """
node_types:
  gcp:
    derived_from: tosca:Root
"""


def test_conditional_imports():
    cli_runner = CliRunner()
    with cli_runner.isolated_filesystem():
        init_project(
            cli_runner,
            args=["init", "--mono", "--skeleton=aws"],
            env=dict(UNFURL_HOME=""),
        )
        with open("ensemble-template.yaml", "w") as f:
            f.write(ensemble)

        with open("aws.yaml", "w") as f:
            f.write(aws_import)

        with open("gcp.yaml", "w") as f:
            f.write(gcp_import)

        manifest = YamlManifest(localEnv=LocalEnv(".", homePath="./unfurl_home"))
        # print_config(".")
        assert "aws" in manifest.tosca.template.topology_template.custom_defs
        assert "gcp" not in manifest.tosca.template.topology_template.custom_defs
