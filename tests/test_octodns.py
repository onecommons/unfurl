from pathlib import Path
from unittest.mock import patch

from moto import mock_route53

from unfurl.job import JobOptions, Runner
from unfurl.support import Status
from unfurl.yamlmanifest import YamlManifest

from .utils import lifecycle


class TestOctoDnsConfigurator:
    def setup(self):
        self.runner = Runner(YamlManifest(ENSEMBLE_ROUTE53))

    @mock_route53
    def test_configure(self):
        job = self.runner.run(JobOptions(workflow="deploy"))

        assert job.status == Status.ok
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        node = job.rootResource.find_resource("test_node")
        assert node.attributes["zone"]["test-domain.com."][""]["type"] == "A"
        assert node.attributes["zone"]["test-domain.com."][""]["values"] == [
            "2.3.4.5",
            "2.3.4.6",
        ]

    @mock_route53
    def test_delete(self):
        self.runner.run(JobOptions(workflow="deploy"))
        job = self.runner.run(JobOptions(workflow="undeploy"))

        assert job.status == Status.ok
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        node = job.rootResource.find_resource("test_node")
        assert node.attributes["zone"]["test-domain.com."] == {}

    @mock_route53
    def test_check(self):
        runner = Runner(YamlManifest(ENSEMBLE_ROUTE53))
        runner.run(JobOptions(workflow="deploy"))
        job = runner.run(JobOptions(workflow="check"))

        assert job.status == Status.ok
        task = list(job.workDone.values())[0]
        # this means that dns records were correctly set during deploy:
        assert task.target_status == Status.ok
        assert task.result.result == {"msg": "DNS records in sync"}

    @mock_route53
    def test_lifecycle(self):
        manifest = YamlManifest(ENSEMBLE_ROUTE53)
        jobs = lifecycle(manifest)
        for job in jobs:
            assert job.status == Status.ok, job.workflow

    @patch("unfurl.configurators.octodns.Manager.sync")
    def test_exclusive(self, manager_sync):
        runner = Runner(YamlManifest(ENSEMBLE_EXCLUSIVE))

        job = runner.run(JobOptions(workflow="deploy"))

        assert job.status == Status.ok
        node = job.rootResource.find_resource("test_node")
        # records are not merged, only ones defined in yaml are used
        assert len(node.attributes["zone"]["test-domain.com."]) == 1
        assert manager_sync.called


DNS_FIXTURE = Path(__file__).parent / "fixtures" / "dns"

ENSEMBLE_ROUTE53 = f"""
apiVersion: unfurl/v1alpha1
kind: Ensemble

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
              class: octodns.provider.route53.Route53Provider
              access_key_id: my_AWS_ACCESS_KEY_ID
              secret_access_key: my_AWS_SECRET_ACCESS_KEY
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
