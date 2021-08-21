import os
import traceback
import unittest
from collections.abc import MutableSequence

import six
import unfurl
from click.testing import CliRunner
from unfurl.__main__ import _args, cli
from unfurl.configurator import Configurator
from unfurl.localenv import LocalEnv
from unfurl.util import sensitive_list
from unfurl.yamlloader import yaml

manifest = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
context:
  locals:
    schema:
      prop2:
       type: number
spec:
  node_templates:
    test:
      type: tosca.nodes.Root
      properties:
        local1:
          eval:
            local: prop1
        local2:
          eval:
            external: local
          select: prop2
        testApikey:
          eval:
           secret: testApikey
        # uses jinja2 native types to evaluate to a list
        aListOfItems:
            eval:
              template: >
                [{% for key, value in aLocalDict.items() %}
                  {
                    'key': '{{ key }}',
                    'value': '{{ value | b64encode }}'
                  },
                {% endfor %}]
            vars:
              aLocalDict:
                eval:
                  secret: aDict
            # trace: 1
      interfaces:
        Standard:
          create: test_cli.CliTestConfigurator
"""

localConfig = """
apiVersion: unfurl/v1alpha1
kind: Project
contexts:
  defaults: #used if manifest isnt found in `manifests` list below
   secrets:
    attributes:
      aDict:
        key1: a string
        key2: 2
      default: # if key isn't found, apply this:
        eval:
          lookup:
            env: "UNFURL_{{ key | upper }}"
  test:
    locals:
      attributes:
        prop1: 'found'
        prop2: 1
        aDict:
          key1: a string
          key2: 2

ensembles:
  - alias: anEnsemble
    file: git/default-manifest.yaml
    context: test
"""


class CliTestConfigurator(Configurator):
    def run(self, task):
        attrs = task.target.attributes
        assert isinstance(attrs["aListOfItems"], MutableSequence), type(
            attrs["aListOfItems"]
        )
        # sort for python2
        assert sorted(attrs["aListOfItems"], key=lambda k: k["key"]) == [
            {"key": "key1", "value": "YSBzdHJpbmc="},
            {"key": "key2", "value": "Mg=="},
        ], attrs["aListOfItems"]
        assert attrs["local1"] == "found", attrs["local1"]
        assert attrs["local2"] == 1, attrs["local2"]
        assert (
            attrs["testApikey"] == "secret"
        ), "failed to get secret environment variable, maybe DelegateAttributes is broken?"

        # list will be marked as sensitive because the template that created referenced a sensitive content
        assert isinstance(attrs["aListOfItems"], sensitive_list), type(
            attrs["aListOfItems"]
        )
        out = six.StringIO()
        yaml.dump(attrs["aListOfItems"], out)
        assert out.getvalue() == "<<REDACTED>>\n...\n", repr(out.getvalue())
        assert isinstance(
            attrs._attributes["aListOfItems"].as_ref(), sensitive_list
        ), type(attrs._attributes["aListOfItems"].as_ref())

        yield task.done(True, False)


class CliTest(unittest.TestCase):
    def test_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, [])
        assert result.output.startswith(
            "Usage: cli [OPTIONS] COMMAND [ARGS]"
        ), result.output
        self.assertEqual(result.exit_code, 0)

    def test_version(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["version"])
        self.assertEqual(result.exit_code, 0, result)
        version = unfurl.__version__(True)
        self.assertIn(version, result.output.strip())

    def test_versioncheck(self):
        self.assertEqual((0, 1, 4, 11), unfurl.version_tuple("0.1.4.dev11"))
        current_version = unfurl.version_tuple()
        if current_version[:3] != (
            0,
            0,
            1,
        ):  # skip if running unit tests on a shallow clone
            assert (
                unfurl.version_tuple("0.1.4.dev11") < current_version
            ), unfurl.version_tuple()
        # XXX this test only passes when run individually -- log output is surpressed otherwise?
        # runner = CliRunner()
        # checkedVersion = "9.0.0"
        # result = runner.invoke(cli, ["-vvv", "--version-check", checkedVersion, "help"])
        # self.assertEqual(result.exit_code, 0, result)
        # self.assertIn(
        #     "older than expected version " + checkedVersion, result.output.strip()
        # )

    @unittest.skipIf(
        "slow" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set"
    )
    def test_runtime(self):
        runner = CliRunner()
        venvSrc = os.path.join(os.path.dirname(__file__), "fixtures/venv")
        repoPath = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
        # instead of runtime = "venv:%s:%s@" % (venvSrc, repoPath)
        # fully specify the runtime so we can point to our fake empty unfurl package
        runtime = "venv:%s:git+file://%s#egg=unfurl&subdirectory=tests/fixtures" % (
            venvSrc,
            repoPath,
        )
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["--runtime=" + runtime, "runtime", "--init"])
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            self.assertEqual(result.exit_code, 0, result)
            self.assertIn("Created runtime", result.output)
            self.assertIn("Installing dependencies from Pipfile.lock", result.output)
            assert os.path.exists(".venv/src/unfurl")
            assert os.path.exists(".venv/bin/unfurl")

            result = runner.invoke(cli, ["init", "--mono", "test"])
            # uncomment this to see output:
            # print("result.output", result.exit_code, result.output)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            self.assertEqual(result.exit_code, 0, result)

            # invoke plan in the runtime we just created
            try:
                if os.environ.get("UNFURL_NORUNTIME"):
                    del os.environ["UNFURL_NORUNTIME"]
                _args[:] = [
                    "--quiet",
                    "--runtime=venv:",
                    "plan",
                    "--output=none",
                    "test",
                ]
                result = runner.invoke(cli, _args)
                # uncomment this to see output:
                # print("result.output", result.exit_code, result.output)
                assert not result.exception, "\n".join(
                    traceback.format_exception(*result.exc_info)
                )
                self.assertEqual(result.exit_code, 0, result)
                self.assertIn("running remote with _args", result.output)
            finally:
                os.environ["UNFURL_NORUNTIME"] = "1"

    @unittest.skipIf(
        "slow" in os.getenv("UNFURL_TEST_SKIP", "")
        or "docker" in os.getenv("UNFURL_TEST_SKIP", ""),
        "UNFURL_TEST_SKIP set",
    )
    def test_docker_runtime(self):
        ensemble = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
configurations:
  create:
    implementation:
      className: unfurl.configurators.shell.ShellConfigurator
    inputs:
      command: "echo hello world"
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
        runner = CliRunner()
        runtime = "docker:onecommons/unfurl:0.2.4"
        _args[:] = [
            f"--runtime={runtime}",
            "deploy",
            "ensemble.yaml",
        ]

        with runner.isolated_filesystem():
            with open("ensemble.yaml", "w") as f:
                f.write(ensemble)
            try:
                if os.environ.get("UNFURL_NORUNTIME"):
                    del os.environ["UNFURL_NORUNTIME"]
                result = runner.invoke(
                    cli, ["--runtime=" + runtime, "deploy", "ensemble.yaml"]
                )
            finally:
                os.environ["UNFURL_NORUNTIME"] = "1"

        # uncomment this to see output:
        # print("result.output", result.exit_code, result.output)
        assert not result.exception, "\n".join(
            traceback.format_exception(*result.exc_info)
        )
        assert result.exit_code == 0, result.stderr
        self.assertIn("running remote with _args", result.output)

    def test_badargs(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--badarg"])
        self.assertEqual(result.exit_code, 2, result)

    def test_run(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            with open("ensemble.yaml", "w") as f:
                f.write("invalid manifest")

            result = runner.invoke(cli, ["run"])
            self.assertEqual(result.exit_code, 1, result)
            # XXX log handler is writing to the CliRunner's output stream
            self.assertIn("Unable to create job", result.output.strip())

            result = runner.invoke(cli, ["run", "--ensemble", "missing.yaml"])
            assert "Ensemble manifest does not exist" in str(
                result.exception
            ), result.exception

            with open("manifest2.yaml", "w") as f:
                f.write(manifest)
            runCmd = ["run", "--ensemble", "manifest2.yaml"]
            result = runner.invoke(cli, runCmd + ["--", "echo", "ok"])
            # print("result.output1!", result.output)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            assert r"'stdout': 'ok\n'" in result.output.replace(
                "u'", "'"
            ), result.output
            # run same command using ansible
            result = runner.invoke(
                cli, runCmd + ["--host", "localhost", "--", "echo", "ok"]
            )
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            assert r"'stdout': 'ok'" in result.output.replace("u'", "'"), result.output

    def test_localConfig(self):
        # test loading the default manifest declared in the local config
        # test locals and secrets:
        #    declared attributes and default lookup
        #    inherited from default (inheritFrom)
        #    verify secret contents isn't saved in config
        os.environ["UNFURL_TESTAPIKEY"] = "secret"
        runner = CliRunner()
        with runner.isolated_filesystem() as tempDir:
            with open("unfurl.yaml", "w") as local:
                local.write(localConfig)
            repoDir = "git"
            os.mkdir(repoDir)
            os.chdir(repoDir)
            with open("default-manifest.yaml", "w") as f:
                f.write(manifest)

            # make sure the test environment set UNFURL_HOME:
            testHomePath = os.environ["UNFURL_HOME"]
            assert testHomePath and testHomePath.endswith(
                "/tmp/unfurl_home"
            ), testHomePath

            # empty UNFURL_HOME disables the home path
            os.environ["UNFURL_HOME"] = ""
            homePath = LocalEnv().homeConfigPath
            assert not homePath, homePath

            # no UNFURL_HOME and the default home path will be used
            del os.environ["UNFURL_HOME"]
            # we don't want to want to try to load the real home path so call getHomeConfigPath() instead
            # homePath = LocalEnv().homeConfigPath
            homePath = unfurl.get_home_config_path(None)
            assert homePath and homePath.endswith(".unfurl_home/unfurl.yaml"), homePath

            # restore test environment's UNFURL_HOME
            os.environ["UNFURL_HOME"] = testHomePath
            homePath = LocalEnv().homeConfigPath
            assert homePath and homePath.startswith(testHomePath), homePath

            homePath = LocalEnv(homePath="").homeConfigPath
            assert homePath is None, homePath

            homePath = LocalEnv(homePath="override").homeConfigPath
            assert homePath and homePath.endswith("/override/unfurl.yaml"), homePath

            # test invoking a named ensemble
            result = runner.invoke(
                cli, ["deploy", "--jobexitcode", "degraded", "anEnsemble"]
            )
            # uncomment this to see output:
            # print("result.output", result.exit_code, result.output)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            self.assertEqual(result.exit_code, 0, result.output)

    @unittest.skipIf(
        "slow" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set"
    )
    def test_clone_project(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            specTemplate = os.path.join(
                os.path.dirname(__file__), "examples/spec/service-template.yaml"
            )
            result = runner.invoke(
                cli, ["--home", "./unfurl_home", "clone", specTemplate, "clone1"]
            )
            # print("result.output", result.exit_code, result.output)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            self.assertEqual(result.exit_code, 0, result)

            result = runner.invoke(
                cli, ["--home", "./unfurl_home", "deploy", "clone1/tests/examples"]
            )
            # print("result.output", result.exit_code, result.output)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            getattr(self, "assertRegex", self.assertRegexpMatches)(
                result.output,
                "Job A[0-9A-Za-z]{11} completed: ok. Found nothing to do.",
            )
            self.assertEqual(result.exit_code, 0, result.output)

            # this will clone the new ensemble
            os.mkdir("anotherdir")
            os.chdir("anotherdir")
            result = runner.invoke(
                cli,
                [
                    "--home",
                    "./unfurl_home",
                    "clone",
                    "../clone1/tests/examples",
                    "clone1",
                ],
            )
            # print("result.output", result.exit_code, result.output)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            self.assertEqual(result.exit_code, 0, result)

            result = runner.invoke(
                cli, ["--home", "./unfurl_home", "deploy", "clone1/tests/examples"]
            )
            # print("result.output", result.exit_code, result.output)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            getattr(self, "assertRegex", self.assertRegexpMatches)(
                result.output,
                "Job A[0-9A-Za-z]{11} completed: ok. Found nothing to do.",
            )
            self.assertEqual(result.exit_code, 0, result.output)

    def test_shared_repo(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            runCmd(runner, ["--home", "./unfurl_home", "init", "shared"])

            runCmd(
                runner,
                ["--home", "./unfurl_home", "init", "--shared-repository=shared", "p1"],
            )
            result = runCmd(runner, ["--home", "./unfurl_home", "deploy", "p1"])
            self.assertRegex(result.output, "Found nothing to do.")

            os.chdir("p1")

            runCmd(
                runner,
                [
                    "--home",
                    "../unfurl_home",
                    "init",
                    "--shared-repository=../shared",
                    "new_ensemble_in_shared",
                ],
            )
            result = runCmd(
                runner, ["--home", "../unfurl_home", "deploy", "new_ensemble_in_share"]
            )
            self.assertRegex(result.output, "Found nothing to do.")

            os.chdir("..")

            runCmd(runner, ["--home", "./unfurl_home", "clone", "p1", "p1copy"])
            result = runCmd(runner, ["--home", "./unfurl_home", "deploy", "p1copy"])
            self.assertRegex(result.output, "Found nothing to do.")


def runCmd(runner, args):
    result = runner.invoke(cli, args)
    # print("result.output", result.exit_code, result.output)
    assert not result.exception, "\n".join(traceback.format_exception(*result.exc_info))
    assert result.exit_code == 0, result
    return result
