import unittest
from giterop.yamlmanifest import YamlManifest
from giterop.job import Runner, JobOptions
from giterop.configurator import Configurator, Status
from giterop.util import lookupPath
import datetime

manifest = '''
apiVersion: giterops/v1alpha1
kind: Manifest
configurations:
  create:
    implementation: giterop.shellconfigurator.ShellConfigurator
    inputs:
     command: "echo 'helloworld'"
     timeout: 9999
     resultTemplate:
        q: |
           - name: .self
             attributes:
               stdout: "{{ stdout | trim }}"
spec:
  node_templates:
    test1:
      type: tosca.nodes.Root
      interfaces:
        Standard:
          +configurations:
'''

class ShellConfiguratorTest(unittest.TestCase):

  def test_shell(self):
    """
    test that runner figures out the proper tasks to run
    """
    runner = Runner(YamlManifest(manifest))

    run1 = runner.run(JobOptions(resource='test1'))
    assert len(run1.workDone) == 1, run1.workDone
    self.assertEqual(runner.manifest.getRootResource().findResource('test1').attributes['stdout'], 'helloworld')
    assert not run1.unexpectedAbort, run1.unexpectedAbort.getStackTrace()
