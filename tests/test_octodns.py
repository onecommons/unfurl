from pathlib import Path
import os
from unittest.mock import patch

from moto import mock_route53

from unfurl.job import JobOptions, Runner
from unfurl.support import Status
from unfurl.yamlmanifest import YamlManifest

from .utils import lifecycle, DEFAULT_STEPS, Step


class TestOctoDnsConfigurator:
    @mock_route53
    def test_configure(self):
        runner = Runner(YamlManifest(ENSEMBLE_ROUTE53))
        job = runner.run(JobOptions(workflow="deploy"))

        assert job.status == Status.ok
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        node = job.rootResource.find_resource("test_node")
        assert node.attributes["zone"][""]["type"] == "A"
        assert node.attributes["zone"][""]["values"] == [
            "2.3.4.5",
            "2.3.4.6",
        ]
        assert node.attributes["zone"]["www"]["values"] == [
            "2.3.4.5",
            "2.3.4.6",
        ]

    @mock_route53
    def test_relationships(self):
        runner = Runner(YamlManifest(ENSEMBLE_WITH_RELATIONSHIPS))
        job = runner.run(JobOptions(workflow="deploy"))

        assert job.status == Status.ok
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        node = job.rootResource.find_resource("test_zone")
        assert node
        assert node.attributes["zone"]["www"]["type"] == "A"
        assert node.attributes["zone"]["www"]["value"] == "10.10.10.1"
        assert node.attributes["managed_records"]["www"]["value"] == "10.10.10.1"

        # if the compute ip address changeses (here via check), the zone should be updated
        try:
            os.environ["OCTODNS_TEST_IP"] = "10.10.10.2"
            job = runner.run(JobOptions(workflow="check"))
        finally:
            del os.environ["OCTODNS_TEST_IP"]

        assert job.status == Status.ok
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()

        compute = job.rootResource.find_resource("compute")
        assert compute
        assert compute.attributes["public_address"] == "10.10.10.2"

        node = job.rootResource.find_resource("test_zone")
        assert node.status == Status.error  # it's now out of sync
        assert node.attributes["zone"]["www"]["value"] == "10.10.10.1"
        assert node.attributes["managed_records"]["www"]["value"] == "10.10.10.2"

        job = runner.run(JobOptions(workflow="undeploy"))
        assert job.status == Status.ok
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        node = job.rootResource.find_resource("test_zone")
        assert dict(node.attributes["zone"]) == {}

    @mock_route53
    def test_lifecycle_relationships(self):
        manifest = YamlManifest(ENSEMBLE_WITH_RELATIONSHIPS)
        steps = list(DEFAULT_STEPS)
        # steps[0] = Step("check", Status.ok)
        jobs = lifecycle(manifest, steps)
        for job in jobs:
            assert job.status == Status.ok, job.workflow

    @mock_route53
    def test_delete(self):
        runner = Runner(YamlManifest(ENSEMBLE_ROUTE53))
        job = runner.run(JobOptions(workflow="deploy"))
        assert job.status == Status.ok
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        node = job.rootResource.find_resource("test_node")
        assert node and len(node.attributes["zone"]) == 2

        job = runner.run(JobOptions(workflow="undeploy"))
        assert job.status == Status.ok
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        node = job.rootResource.find_resource("test_node")
        assert dict(node.attributes["zone"]) == {}

    @mock_route53
    def test_check(self):
        runner = Runner(YamlManifest(ENSEMBLE_ROUTE53))
        runner.run(JobOptions(workflow="deploy"))
        job = runner.run(JobOptions(workflow="check"))

        assert job.status == Status.ok
        task = list(job.workDone.values())[0]
        # this means that dns records were correctly set during deploy:
        assert task.target_status == Status.ok
        assert task.result.result == "DNS records in sync"

    @mock_route53
    def test_lifecycle(self):
        manifest = YamlManifest(ENSEMBLE_ROUTE53)
        jobs = lifecycle(manifest)
        for job in jobs:
            assert job.status == Status.ok, job.workflow

    @patch("unfurl.configurators.dns.Manager.sync")
    def test_exclusive(self, manager_sync):
        runner = Runner(YamlManifest(ENSEMBLE_EXCLUSIVE))

        job = runner.run(JobOptions(workflow="deploy"))

        assert job.status == Status.ok
        node = job.rootResource.find_resource("test_node")
        # records are replaced by instance
        assert len(node.attributes["zone"]) == 1
        assert manager_sync.called

    @mock_route53
    def test_lifecycle_exclusive(self):
        manifest = YamlManifest(
            ENSEMBLE_ROUTE53.replace("exclusive: false", "exclusive: true")
        )
        jobs = lifecycle(manifest)
        for job in jobs:
            assert job.rootResource.find_resource("test_node").attributes["exclusive"]
            assert job.status == Status.ok, job.workflow


DNS_FIXTURE = Path(__file__).parent / "fixtures" / "dns"

ENSEMBLE_ROUTE53 = """
apiVersion: unfurl/v1alpha1
kind: Ensemble

spec:
  service_template:
    imports:
      - repository: unfurl
        file: configurators/dns-template.yaml

    topology_template:
      node_templates:
        test_node:
          type: unfurl.nodes.DNSZone
          properties:
            name: test-domain.com.
            exclusive: false
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
              www:
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
        file: configurators/dns-template.yaml

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

ENSEMBLE_WITH_RELATIONSHIPS = """
apiVersion: unfurl/v1alpha1
kind: Ensemble

spec:
  service_template:
    imports:
      - repository: unfurl
        file: configurators/dns-template.yaml

    topology_template:
      node_templates:
        test_zone:
          type: unfurl.nodes.DNSZone
          properties:
            name: test-domain.com.
            provider:
              class: octodns.provider.route53.Route53Provider
              access_key_id: my_AWS_ACCESS_KEY_ID
              secret_access_key: my_AWS_SECRET_ACCESS_KEY

        test_app:
          type: tosca.nodes.WebServer
          requirements:
            - host: compute
            - dns:
                node: test_zone
                relationship:
                   type:   unfurl.relationships.DNSRecords
                   properties:
                     records:
                      www:
                        type: A
                        value:
                          eval: .source::.requirements::[.name=host]::.target::public_address
        compute:
          type: tosca.nodes.Compute
          interfaces:
             Install:
              operations:
                check:
                  inputs:
                    done:
                      status: "{%if '.status' | eval == 4 %}absent{%endif%}"
             Standard:
              operations:
                create:
                delete:
                  inputs:
                    done:
                      status: absent
             defaults:
                  implementation: Template
                  inputs:
                    done:
                      status: ok
                    resultTemplate: |
                      - name: .self
                        attributes:
                          public_address: {get_env: [OCTODNS_TEST_IP, 10.10.10.1]}

"""
