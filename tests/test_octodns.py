from pathlib import Path

from unfurl.job import Runner, JobOptions
from unfurl.support import Status
from unfurl.yamlmanifest import YamlManifest


class TestOctoDnsConfigurator:
    def test_simple(self):
        runner = Runner(YamlManifest(ENSEMBLE_SIMPLE))

        job = runner.run(JobOptions(instance="test_node", dryrun=True))

        assert job.status == Status.ok
        node = job.rootResource.find_resource("test_node")
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        assert node.attributes["dns_zones"]["test-domain.com."][""]["type"] == "A"
        assert node.attributes["dns_zones"]["test-domain.com."][""]["values"] == [
            "2.3.4.5",
            "2.3.4.6",
        ]


DNS_FIXTURE = Path(__file__).parent / "fixtures" / "dns"
ENSEMBLE_SIMPLE = f"""
apiVersion: unfurl/v1alpha1
kind: Ensemble
configurations:
  create:
    implementation:
      className: unfurl.configurators.octodns.OctoDnsConfigurator
    inputs:
      workers: 2
      dump_providers:
        - dump
      main-config:
        manager:
          max_workers: '{{{{ inputs.workers }}}}'
        providers:
          config:
            class: octodns.provider.yaml.YamlProvider
            directory: ./
          target_config:
            class: octodns.provider.yaml.YamlProvider
            directory: ./target/
          dump:
            class: octodns.source.axfr.ZoneFileSource
            directory: {DNS_FIXTURE}
            file_extension: .tst
        zones:
          test-domain.com.:
            sources:
              - config
            targets:
              - target_config
      zones:
        test-domain.com.:
          '':
            ttl: 60
            type: A
            values:
              - 2.3.4.5
              - 2.3.4.6

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
