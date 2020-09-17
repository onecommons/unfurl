import unittest
from unfurl.yamlmanifest import YamlManifest
from unfurl.job import Runner, JobOptions
from unfurl.configurator import Configurator, Status
from unfurl.merge import lookupPath
import datetime

manifest = """
apiVersion: unfurl/v1alpha1
kind: Manifest
configurations:
  create:
    implementation:
      className: unfurl.configurators.shell.ShellConfigurator
      environment:
        FOO: "{{inputs.foo}}"
    inputs:
       # test that self-references works in jinja2 templates
       command: "echo ${{inputs.envvar}}"
       timeout: 9999
       foo:     helloworld
       envvar:  FOO
       resultTemplate: |
         - name: SELF
           attributes:
             stdout: "{{ stdout | trim }}"
spec:
  service_template:
    topology_template:
      node_templates:
        test1:
          type: tosca.nodes.Root
          interfaces:
            Standard:
              +/configurations:
"""


class ShellConfiguratorTest(unittest.TestCase):
    def test_shell(self):
        """
    test that runner figures out the proper tasks to run
    """
        runner = Runner(YamlManifest(manifest))

        run1 = runner.run(JobOptions(instance="test1"))
        assert len(run1.workDone) == 1, run1.workDone
        self.assertEqual(
            runner.manifest.getRootResource()
            .findResource("test1")
            .attributes["stdout"],
            "helloworld",
        )
        assert not run1.unexpectedAbort, run1.unexpectedAbort.getStackTrace()
