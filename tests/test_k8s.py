import unfurl.util
import unittest

from unfurl.yamlmanifest import YamlManifest
from unfurl.job import Runner, JobOptions
from unfurl.runtime import Status
import os
import os.path
import warnings

manifestScript = """
apiVersion: unfurl/v1alpha1
kind: Manifest
vars:
  defaultNamespace:
    requirements:
       - host: k8sNamespace
spec:
  tosca:
    dsl_definitions:
    topology_template:
      inputs:
        kubeConnection:
          type: map
          default:
            context: docker-for-desktop
      node_templates:
        k8sCluster:
         type: unfurl.nodes.K8sCluster
         properties:
           connection: { get_input: kubeConnection }
        k8sNamespace:
         type: unfurl.nodes.K8sNamespace
         requirements:
           - host: k8sCluster
         properties:
           name: octest
        testSecret:
         # add metadata, type: Opaque
         # base64 values and omit data from status
         type: unfurl.nodes.k8sSecretResource
         requirements:
           - host: k8sNamespace
         properties:
             name: test-secret
             data:
               uri: "{{ lookup('env', 'TEST_SECRET') }}"
"""


class k8sTest(unittest.TestCase):
    def setUp(self):
        # need to call this again on python 2.7:
        unfurl.util.initializeAnsible()
        try:
            # Ansible generates tons of ResourceWarnings
            warnings.simplefilter("ignore", ResourceWarning)
        except:
            # python 2.x doesn't have ResourceWarning
            pass

    def test_k8sConfig(self):
        os.environ["TEST_SECRET"] = "a secret"
        manifest = YamlManifest(manifestScript)
        job = Runner(manifest).run(JobOptions(add=True, startTime="time-to-test"))
        # print(job.summary())
        # verify secret contents isn't saved in config
        self.assertIn("uri: '[[REDACTED]]'", job.out.getvalue())
        self.assertNotIn("a secret", job.out.getvalue())
        assert not job.unexpectedAbort
        assert job.status == Status.ok, job.summary()

    # def test_Delete(self):
    #   manifest = YamlManifest(manifestScript)
    #   job = Runner(manifest).run(JobOptions(remove=True, startTime="time-to-test"))
    #   print(job.out.getvalue())
    #   print(job.summary())
    #   assert not job.unexpectedAbort
    #   assert job.status == Status.ok, job.summary()
