import unittest
from giterop.manifest import YamlManifest
from giterop.runtime import Runner, Configurator, JobOptions, Status
from giterop.util import lookupPath
import datetime
import logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger('gitup')
logger.setLevel(logging.DEBUG)

# XXX giterops!!
manifest = '''
apiVersion: giterops/v1alpha1
kind: Manifest
configurations:
  test:
    apiVersion: giterops/v1alpha1
    className: giterop.shellconfigurator.ShellConfigurator
    majorVersion: 0
    parameters:
     command: "echo 'helloworld'"
     timeout: 9999
     resultTemplate:
        q: |
           - name: .self
             status:
              attributes:
               stdout: "{{ stdout | trim }}"

root:
  resources:
    test1:
      spec:
        configurations:
          +configurations:
'''

class ShellConfiguratorTest(unittest.TestCase):

  def test_shell(self):
    """
    test that runner figures out the proper tasks to run
    """
    runner = Runner(YamlManifest(manifest))

    run1 = runner.run(JobOptions(resource='test1'))
    self.assertEqual(runner.manifest.getRootResource().findResource('test1').attributes['stdout'], 'helloworld')
    assert len(run1.workDone) == 1, run1.workDone
    assert not run1.unexpectedAbort, run1.unexpectedAbort.getStackTrace()
