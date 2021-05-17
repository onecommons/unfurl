import os
import unittest

from unfurl.job import JobOptions, Runner
from unfurl.yamlmanifest import YamlManifest


class ShellConfiguratorTest(unittest.TestCase):
    def setUp(self):
        path = os.path.join(
            os.path.dirname(__file__), "examples", "shell-ensemble.yaml"
        )
        with open(path) as f:
            self.manifest = f.read()

    def test_shell(self):
        """
        test that runner figures out the proper tasks to run
        """
        runner = Runner(YamlManifest(self.manifest))

        run1 = runner.run(JobOptions(instance="test1"))
        assert len(run1.workDone) == 1, run1.workDone
        self.assertEqual(
            runner.manifest.getRootResource()
            .findResource("test1")
            .attributes["stdout"],
            "helloworld",
        )
        assert not run1.unexpectedAbort, run1.unexpectedAbort.getStackTrace()
