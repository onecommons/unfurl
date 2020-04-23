import unittest
import os
import traceback
from click.testing import CliRunner
from unfurl.__main__ import cli
from unfurl import __version__
from git import Repo
from unfurl.configurator import Configurator, Status


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
