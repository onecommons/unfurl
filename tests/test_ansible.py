import os
import sys
import warnings
from pathlib import Path

import pytest

from unfurl.configurators.ansible import run_playbooks
from unfurl.job import JobOptions, Runner
from unfurl.runtime import Status
from unfurl.yamlmanifest import YamlManifest

from .utils import isolated_lifecycle

if not sys.warnoptions:
    # Ansible generates tons of ResourceWarnings
    warnings.simplefilter("ignore", ResourceWarning)


class AnsibleTest:
    def setup(self):
        self.results = {}

    @staticmethod
    def run_playbook(args=None):
        return run_playbooks(
            str(Path(__file__).parent / "examples" / "testplaybook.yaml"),
            "localhost,",
            {
                "ansible_connection": "local",
                "extra": 1,
                "ansible_python_interpreter": sys.executable,  # suppress ansible warning
            },
            args,
        )

    def test_run_playbook(self):
        results = self.run_playbook()
        self.results["runplaybook"] = results
        facts = results.variableManager._nonpersistent_fact_cache["localhost"]
        assert facts.get("one_fact") == "test"

        hostfacts = results.variableManager._nonpersistent_fact_cache["localhost"]
        assert hostfacts["one_fact"] == "test"
        assert hostfacts["echoresults"]["stdout"] == "hello"

    # setting UNFURL_LOGGING can break this test, so skip if it's set
    @pytest.mark.skipif(
        os.getenv("UNFURL_LOGGING"), reason="this test requires default log level"
    )
    def test_verbosity(self):
        results = self.run_playbook()
        # task test-verbosity was skipped
        assert not results.resultsByStatus.ok.get("test-verbosity")
        assert results.resultsByStatus.skipped.get("test-verbosity")
        results = self.run_playbook(["-vv"])
        # task test-verbosity was ok this time
        assert results.resultsByStatus.ok.get("test-verbosity")
        assert not results.resultsByStatus.skipped.get("test-verbosity")


class AnsibleConfiguratorTest:
    def setup(self):
        path = Path(__file__).parent / "examples" / "ansible-simple-ensemble.yaml"
        with open(path) as f:
            self.manifest = f.read()

    def test_configurator(self):
        """
        test that runner figures out the proper tasks to run
        """
        runner = Runner(YamlManifest(self.manifest))
        run1 = runner.run(JobOptions(resource="test1"))
        assert not run1.unexpectedAbort, run1.unexpectedAbort.get_stack_trace()
        assert len(run1.workDone) == 1, run1.workDone
        result = list(run1.workDone.values())[0].result
        assert result.outputs == {"fact1": "test1", "fact2": "test"}
        assert result.result.get("stdout") == sys.executable
        assert run1.status == Status.ok, run1.summary()


class TestAnsibleLifecycle:
    def test_lifecycle(self):
        path = Path(__file__).parent / "examples" / "ansible-simple-ensemble.yaml"
        jobs = isolated_lifecycle(str(path))
        for job in jobs:
            assert job.status == Status.ok, job.workflow
