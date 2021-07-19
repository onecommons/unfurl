from unittest.mock import patch

from unfurl.job import Runner, JobOptions
from unfurl.support import Status
from unfurl.yamlmanifest import YamlManifest


class TestOctoDnsConfigurator:
    @patch("unfurl.configurators.octodns.Manager")
    @patch("unfurl.configurators.octodns.OctoDnsConfigurator._read_current_dns_records")
    def test_simple(self, read_dns_records, manager):
        runner = Runner(YamlManifest(ENSEMBLE_SIMPLE))
        read_dns_records.return_value = {"test-domain.com.": {}}

        job = runner.run(JobOptions(instance="test_node", dryrun=True))

        assert job.status == Status.ok
        node = job.rootResource.find_resource("test_node")
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        assert "dns_zones" in node.attributes


ENSEMBLE_SIMPLE = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
configurations:
  create:
    implementation:
      className: unfurl.configurators.octodns.OctoDnsConfigurator
    inputs:
      aws_key: MY_AWS_SECRET
      main-config: |
        ---
        manager:
          max_workers: 2

        providers:
          config:
            class: octodns.provider.yaml.YamlProvider
            directory: ./
            default_ttl: 3600
            enforce_order: True
          route53:
            class: octodns.provider.route53.Route53Provider
            access_key_id: {{ inputs.aws_key }}
            secret_access_key: MY_ACCESS_SECRET

        zones:
          test-domain.com.:
            sources:
              - config
            targets:
              - route53
      zones:
        test-domain.com.: |
          ---
          '':
            ttl: 60
            type: A
            values:
              - 1.2.3.4
              - 1.2.3.5

spec:
  service_template:
    topology_template:
      node_templates:
        test_node:
          type: tosca.nodes.Root
          interfaces:
            Standard:
              +/configurations:
"""
