import os
import sys
import unittest
import warnings

import pytest

from unfurl.job import JobOptions, Runner
from unfurl.runtime import Status
from unfurl.yamlmanifest import YamlManifest

from .utils import lifecycle

if not sys.warnoptions:
    # Ansible generates tons of ResourceWarnings
    warnings.simplefilter("ignore", ResourceWarning)


@pytest.mark.skipif(
    "k8s" in os.getenv("UNFURL_TEST_SKIP", ""), reason="UNFURL_TEST_SKIP set"
)
class TestK8s(unittest.TestCase):
    def test_k8s_config(self):
        os.environ["TEST_SECRET"] = "a secret"
        manifest = YamlManifest(MANIFEST)
        job = Runner(manifest).run(JobOptions(add=True, startTime=1))
        assert not job.unexpectedAbort
        assert job.status == Status.ok, job.summary()
        # print(job.summary())
        # print(job.out.getvalue())

        # verify secret contents isn't saved in config
        assert "a secret" not in job.out.getvalue()
        assert "YSBzZWNyZXQ" not in job.out.getvalue()  # base64 of "a secret"
        # print (job.out.getvalue())
        assert "<<REDACTED>>" in job.out.getvalue()
        assert not job.unexpectedAbort
        assert job.status == Status.ok, job.summary()

        manifest = YamlManifest(job.out.getvalue())
        job2 = Runner(manifest).run(JobOptions(workflow="undeploy", startTime=2))
        results = job2.json_summary()
        assert not job2.unexpectedAbort
        assert job2.status == Status.ok, job2.summary()
        assert results == {
            "job": {
                "id": "A01120000000",
                "status": "ok",
                "total": 2,
                "ok": 2,
                "error": 0,
                "unknown": 0,
                "skipped": 0,
                "changed": 2,
            },
            "outputs": {},
            "tasks": [
                {
                    "status": "ok",
                    "target": "testSecret",
                    "operation": "delete",
                    "template": "testSecret",
                    "type": "unfurl.nodes.K8sSecretResource",
                    "targetStatus": "absent",
                    "targetState": "deleted",
                    "changed": True,
                    "configurator": "unfurl.configurators.k8s.ResourceConfigurator",
                    "priority": "required",
                    "reason": "undeploy",
                },
                {
                    "status": "ok",
                    "target": "k8sNamespace",
                    "operation": "delete",
                    "template": "k8sNamespace",
                    "type": "unfurl.nodes.K8sNamespace",
                    "targetStatus": "absent",
                    "targetState": "deleted",
                    "changed": True,
                    "configurator": "unfurl.configurators.k8s.ResourceConfigurator",
                    "priority": "required",
                    "reason": "undeploy",
                },
            ],
        }
        assert len(results["tasks"]) == 2, results


MANIFEST = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
spec:
  service_template:
    dsl_definitions:
    topology_template:
      relationship_templates:
        k8sConnection:
          # if a template defines node or capability it will be used
          # as the default relationship when connecting to that node
          default_for: ANY
          # target: k8sCluster
          type: unfurl.relationships.ConnectsTo.K8sCluster
          properties:
            context: {get_env: [UNFURL_TEST_KUBECONTEXT]}
            KUBECONFIG: {get_env: UNFURL_TEST_KUBECONFIG}

      node_templates:
        k8sCluster:
          type: unfurl.nodes.K8sCluster
          directives:
            - discover

        k8sNamespace:
         type: unfurl.nodes.K8sNamespace
         requirements:
           - host: k8sCluster
         properties:
           name: octest

        testSecret:
         # add metadata, type: Opaque
         # base64 values and omit data from status
         type: unfurl.nodes.K8sSecretResource
         requirements:
           - host: k8sNamespace
         properties:
             name: test-secret
             data:
               uri: "{{ lookup('env', 'TEST_SECRET') }}"
"""
