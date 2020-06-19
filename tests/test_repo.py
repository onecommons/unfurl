import unittest
import os
import traceback
from click.testing import CliRunner
from unfurl.__main__ import cli, _latestJobs
from unfurl import __version__
from git import Repo
from unfurl.configurator import Configurator, Status
import unfurl.configurators  # python2.7 workaround


def createUnrelatedRepo(gitDir):
    os.makedirs(gitDir)
    repo = Repo.init(gitDir)
    filename = "README"
    filepath = os.path.join(gitDir, filename)
    with open(filepath, "w") as f:
        f.write("""just another git repository""")

    repo.index.add([filename])
    repo.index.commit("Initial Commit")
    return repo


class AConfigurator(Configurator):
    def run(self, task):
        assert self.canRun(task)
        yield task.done(True, Status.ok)


manifestContent = """\
  apiVersion: unfurl/v1alpha1
  kind: Manifest
  spec:
    service_template:
      +include: service-template.yaml
      topology_template:
        node_templates:
          my_server:
            type: tosca.nodes.Compute
            interfaces:
             Standard:
              create: AConfigurator
  status: {}
  """

awsTestManifest = """\
  apiVersion: unfurl/v1alpha1
  kind: Manifest
  context:
    environment:
      AWS_ACCESS_KEY_ID: mockAWS_ACCESS_KEY_ID
    external:
      localhost:
        connections:
          aws: aws_test
  spec:
    service_template:
      +include: service-template.yaml
      topology_template:
        node_templates:
          testNode:
            type: tosca.nodes.Root
            interfaces:
             Install:
              operations:
                check:
                  implementation:
                    className: unfurl.configurators.TemplateConfigurator
                  inputs:
                    # test that the aws connection (defined in the home manifest and renamed in the context to aws_test)
                    # set its AWS_ACCESS_KEY_ID to the environment variable
                    resultTemplate: |
                      - name: SELF
                        attributes:
                          access_key: {{ "$connections::aws_test::AWS_ACCESS_KEY_ID" | eval }}
  """


class SharedGitRepoTest(unittest.TestCase):
    """
    test that .gitignore, unfurl.local.example.yaml is created
    test that init cmd committed the project config and related files
    """

    def test_init(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            repoDir = "./arepo"
            repo = createUnrelatedRepo(repoDir)
            os.chdir(repoDir)
            # override home so to avoid interferring with other tests
            result = runner.invoke(
                cli, ["--home", "../unfurl_home", "init", "--existing", "deploy_dir"]
            )
            # uncomment this to see output:
            # print("result.output", result.exit_code, result.output)

            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            self.assertEqual(result.exit_code, 0, result)
            expectedFiles = {
                "unfurl.yaml",
                "service-template.yaml",
                ".gitignore",
                "manifest.yaml",
                "unfurl.local.example.yaml",
            }
            self.assertEqual(set(os.listdir("deploy_dir")), expectedFiles)
            self.assertEqual(
                set(repo.head.commit.stats.files.keys()),
                {"deploy_dir/" + f for f in expectedFiles},
            )
            # for n in expectedFiles:
            #     with open("deploy_dir/" + n) as f:
            #         print(n)
            #         print(f.read())

            with open("deploy_dir/manifest.yaml", "w") as f:
                f.write(manifestContent)

            args = ["-vvv", "deploy", "deploy_dir", "--jobexitcode", "degraded"]
            result = runner.invoke(cli, args)
            # print("result.output", result.exit_code, result.output)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            self.assertEqual(result.exit_code, 0, result)

    def test_home_manifest(self):
        """
        test that we can connect to AWS account
        """
        runner = CliRunner()
        with runner.isolated_filesystem():
            # override home so to avoid interferring with other tests
            result = runner.invoke(cli, ["--home", "./unfurl_home", "init", "--mono"])
            # uncomment this to see output:
            # print("result.output", result.exit_code, result.output)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )

            with open("manifest.yaml", "w") as f:
                f.write(awsTestManifest)
            args = [
                "-vvv",
                "--home",
                "./unfurl_home",
                "check",
                "--jobexitcode",
                "degraded",
            ]
            result = runner.invoke(cli, args)
            # print("result.output", result.exit_code, result.output)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            self.assertEqual(result.exit_code, 0, result)

            assert _latestJobs
            job = _latestJobs[-1]
            access_key = job.rootResource.findResource("testNode").attributes[
                "access_key"
            ]
            self.assertEqual(access_key, "mockAWS_ACCESS_KEY_ID")

            # tasks = list(job.workDone.values())
            # print("task", tasks[0].summary())
            # print("job", job.stats(), job.getOutputs())
            # self.assertEqual(job.status.name, "ok")
            # self.assertEqual(job.stats()["ok"], 1)
            # self.assertEqual(job.getOutputs()["aOutput"], "set")
