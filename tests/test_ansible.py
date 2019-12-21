import unfurl.util
import unittest
from unfurl.yamlmanifest import YamlManifest
from unfurl.job import Runner, JobOptions
from unfurl.configurators.ansible import runPlaybooks
from unfurl.runtime import Status
import os
import os.path
import warnings
import sys


class AnsibleTest(unittest.TestCase):
    def setUp(self):
        # need to call this again on python 2.7:
        unfurl.util.initializeAnsible()
        try:
            # Ansible generates tons of ResourceWarnings
            warnings.simplefilter("ignore", ResourceWarning)
        except:
            # python 2.x doesn't have ResourceWarning
            pass
        self.results = {}

    def runPlaybook(self, args=None):
        return runPlaybooks(
            [os.path.join(os.path.dirname(__file__), "testplaybook.yaml")],
            "localhost,",
            {"ansible_connection": "local", "extra": 1},
            args,
        )

    def test_runplaybook(self):
        results = self.runPlaybook()
        self.results["runplaybook"] = results
        self.assertEqual(
            "test",
            results.variableManager._nonpersistent_fact_cache["localhost"].get(
                "one_fact"
            ),
        )

        hostfacts = results.variableManager._nonpersistent_fact_cache["localhost"]
        self.assertEqual(hostfacts["one_fact"], "test")
        self.assertEqual(hostfacts["echoresults"]["stdout"], "hello")

    def test_verbosity(self):
        # setting UNFURL_LOGGING can break this test, so skip if it's set
        if not os.getenv("UNFURL_LOGGING"):
            results = self.runPlaybook()
            # task test-verbosity was skipped
            assert not results.resultsByStatus.ok.get("test-verbosity")
            assert results.resultsByStatus.skipped.get("test-verbosity")
            results = self.runPlaybook(["-vv"])
            # task test-verbosity was ok this time
            assert results.resultsByStatus.ok.get("test-verbosity")
            assert not results.resultsByStatus.skipped.get("test-verbosity")


manifest = """
apiVersion: unfurl/v1alpha1
kind: Manifest
configurations:
  create:
    implementation: unfurl.configurators.ansible.AnsibleConfigurator
    inputs:
      playbook:
        q:
          - name: Hello
            command: echo "{{hostvars['localhost'].ansible_python_interpreter}}"
spec:
  node_templates:
    test1:
      type: tosca.nodes.Root
      interfaces:
        Standard:
          +/configurations:
"""


class AnsibleConfiguratorTest(unittest.TestCase):
    def setUp(self):
        try:
            # Ansible generates tons of ResourceWarnings
            warnings.simplefilter("ignore", ResourceWarning)
        except:
            # python 2.x doesn't have ResourceWarning
            pass

    def test_ansible(self):
        """
    test that runner figures out the proper tasks to run
    """
        runner = Runner(YamlManifest(manifest))
        run1 = runner.run(JobOptions(resource="test1"))
        assert not run1.unexpectedAbort, run1.unexpectedAbort.getStackTrace()
        assert len(run1.workDone) == 1, run1.workDone
        result = list(run1.workDone.values())[0].result.result
        self.assertEqual(result, {"returncode": 0, "stdout": sys.executable})
        assert run1.status == Status.ok, run1.summary()
