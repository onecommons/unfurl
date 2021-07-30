from pathlib import Path
from unittest.mock import patch

from unfurl.job import Runner, JobOptions
from unfurl.support import Status
from unfurl.yamlmanifest import YamlManifest


class TestOctoDnsConfigurator:
    def setup(self):
        self.runner = Runner(YamlManifest(ENSEMBLE))

    @patch("unfurl.configurators.octodns.Manager.sync")
    @patch("unfurl.configurators.octodns.OPERATION", "configure")
    def test_configure(self, manager_sync):
        job = self.runner.run(JobOptions(instance="test_node", dryrun=True))

        assert job.status == Status.ok
        node = job.rootResource.find_resource("test_node")
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        assert node.attributes["zone"]["test-domain.com."][""]["type"] == "A"
        assert node.attributes["zone"]["test-domain.com."][""]["values"] == [
            "2.3.4.5",
            "2.3.4.6",
        ]
        assert manager_sync.called

    @patch("unfurl.configurators.octodns.Manager.sync")
    @patch("unfurl.configurators.octodns.OPERATION", "delete")
    def test_delete(self, manager_sync):
        job = self.runner.run(JobOptions(instance="test_node", dryrun=True))

        assert job.status == Status.ok
        node = job.rootResource.find_resource("test_node")
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        assert node.attributes["zone"]["test-domain.com."] == {}
        assert manager_sync.called

    @patch("unfurl.configurators.octodns.OPERATION", "check")
    def test_check(self):
        job = self.runner.run(
            JobOptions(instance="test_node", dryrun=True, operation="check")
        )

        assert job.status == Status.error

    @patch("unfurl.configurators.octodns.Manager.sync")
    @patch("unfurl.configurators.octodns.OPERATION", "configure")
    def test_exclusive(self, manager_sync):
        runner = Runner(YamlManifest(ENSEMBLE_EXCLUSIVE))

        job = runner.run(JobOptions(instance="test_node", dryrun=True))

        assert job.status == Status.ok
        node = job.rootResource.find_resource("test_node")
        # records are not merged, only ones defined in yaml are used
        assert len(node.attributes["zone"]["test-domain.com."]) == 1


DNS_FIXTURE = Path(__file__).parent / "fixtures" / "dns"

ENSEMBLE = f"""
apiVersion: unfurl/v1alpha1
kind: Ensemble
tosca_definitions_version: tosca_simple_unfurl_1_0_0

spec:
  service_template:
    imports:
      - repository: unfurl
        file: configurators/octodns-template.yaml

    topology_template:
      node_templates:
        test_node:
          type: unfurl.nodes.DNSZone
          properties:
            name: test-domain.com.
            provider:
              class: octodns.source.axfr.ZoneFileSource
              directory: {DNS_FIXTURE}
              file_extension: .tst
            records:
              '':
                ttl: 60
                type: A
                values:
                  - 2.3.4.5
                  - 2.3.4.6
"""

ENSEMBLE_EXCLUSIVE = f"""
apiVersion: unfurl/v1alpha1
kind: Ensemble
tosca_definitions_version: tosca_simple_unfurl_1_0_0

spec:
  service_template:
    imports:
      - repository: unfurl
        file: configurators/octodns-template.yaml

    topology_template:
      node_templates:
        test_node:
          type: unfurl.nodes.DNSZone
          properties:
            name: test-domain.com.
            exclusive: true
            provider:
              class: octodns.source.axfr.ZoneFileSource
              directory: {DNS_FIXTURE}
              file_extension: .tst
            records:
              '':
                type: A
                values:
                  - 2.3.4.5
                  - 2.3.4.6
"""
