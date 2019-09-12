import giterop.util
import unittest

from giterop.yamlmanifest import YamlManifest
from giterop.job import Runner, JobOptions
from giterop.runtime import Status
import os
import os.path
import warnings

manifestScript = """
apiVersion: giterops/v1alpha1
kind: Manifest
spec:
  tosca:
    topology_template:
      inputs:
        kubeConnection:
          type: any
          default:
            context: docker-for-desktop
      node_templates:
        k8sCluster:
         type: giterop.nodes.K8sCluster
         properties:
           connection: { get_input: kubeConnection }
        k8sNamespace:
         type: giterop.nodes.K8sNamespace
         requirements:
           - host: k8sCluster
         properties:
           name: octest
        testSecret:
         # add metadata, type: Opaque
         # base64 values and omit data from status
         type: giterop.nodes.k8sSecretResource
         requirements:
           - host: k8sNamespace
         properties:
             name: test-secret
             data:
               uri: "{{ lookup('env', 'TEST_SECRET') }}" # XXX make secret
"""

class k8sTest(unittest.TestCase):
  def setUp(self):
    # need to call this again on python 2.7:
    giterop.util.initializeAnsible()
    try:
      # Ansible generates tons of ResourceWarnings
      warnings.simplefilter("ignore", ResourceWarning)
    except:
      # python 2.x doesn't have ResourceWarning
      pass

  def test_k8sConfig(self):
    # test loading the default manifest declared in the local config
    # test locals and secrets:
    #    declared attributes and default lookup
    #    inherited from default (inheritFrom)
    #    verify secret contents isn't saved in config
    # [{'k8s': {'state': 'present', 'definition': {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': 'octest'}}, 'context': 'docker-for-desktop'}}]
    os.environ['TEST_SECRET'] = 'secret'
    manifest = YamlManifest(manifestScript)
    job = Runner(manifest).run(JobOptions(add=True, startTime="time-to-test"))
    # print(job.summary())
    assert job.status == Status.ok, job.summary()
