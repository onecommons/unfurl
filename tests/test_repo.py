import unittest
import os
import traceback
import pytest
from click.testing import CliRunner
from unfurl.__main__ import cli, _latestJobs
from unfurl.localenv import LocalEnv
from unfurl.projectpaths import rmtree
from unfurl.repo import (
    split_git_url,
    is_url_or_git_path,
    RepoView,
    normalize_git_url,
    GitRepo,
)
from git import Repo
from unfurl.configurator import Configurator, Status
from toscaparser.common.exception import URLException
from unfurl.testing import run_cmd
from unfurl.yamlmanifest import YamlManifest
from .utils import print_config


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


installDirs = [
    ("terraform", "0.13.6", "bin"),
    ("gcloud", "313.0.0", "bin"),
    ("helm", "3.3.4", "bin"),
]


def makeAsdfFixtures(base):
    # make install dirs so we can pretend we already downloaded these
    for subdir in installDirs:
        path = os.path.join(base, "plugins", subdir[0])
        if not os.path.exists(path):
            os.makedirs(path)
        path = os.path.join(base, "installs", *subdir)
        if not os.path.exists(path):
            os.makedirs(path)


class AConfigurator(Configurator):
    def run(self, task):
        assert self.can_run(task)
        yield task.done(True, Status.ok)


manifestContent = """\
  apiVersion: unfurl/v1alpha1
  kind: Ensemble
  +include:
    file: ensemble-template.yaml
    repository: spec
  spec:
    service_template:
      topology_template:
        node_templates:
          my_server:
            type: tosca.nodes.Compute
            interfaces:
             Standard:
              create: A
  status: {}
  """

awsTestManifest = """\
  apiVersion: unfurl/v1alpha1
  kind: Ensemble
  +include:
    file: ensemble-template.yaml
    repository: spec
  environment:
    variables:
      AWS_ACCESS_KEY_ID: mockAWS_ACCESS_KEY_ID
    connections:
      aws_test: primary_provider
  changes: [] # set this so we save changes here instead of the job changelog files
  spec:
    service_template:
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
                    # set its AWS_ACCESS_KEY_ID to the environment variable set in the current context
                    resultTemplate: |
                      - name: SELF
                        attributes:
                          # all connections available to the OPERATION_HOST as a dictionary
                          access_key: {{ "$connections::aws_test::AWS_ACCESS_KEY_ID" | eval }}
                          # the current connections between the OPERATION_HOST and the target or the target's HOSTs
                          access_key2: {{ "$connections::*::AWS_ACCESS_KEY_ID?" | eval }}
                          access_key3: {{ "$connections::AWSAccount::AWS_ACCESS_KEY_ID" | eval }}
  """


class GitRepoTest(unittest.TestCase):
    """
    test that .gitignore, local/unfurl.yaml is created
    test that init cmd committed the project config and related files
    """

    def test_init_in_existing_repo(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            repoDir = "./arepo"
            repo = createUnrelatedRepo(repoDir)
            os.chdir(repoDir)
            # override home so to avoid interfering with other tests
            result = runner.invoke(
                cli,
                [
                    "--home",
                    "../unfurl_home",
                    "init",
                    "--existing",
                    "--mono",
                    "deploy_dir",
                ],
            )
            # uncomment this to see output:
            # print("result.output", result.exit_code, result.output)

            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            self.assertEqual(result.exit_code, 0, result)
            expectedCommittedFiles = {
                "unfurl.yaml",
                ".unfurl-local-template.yaml",
                "ensemble-template.yaml",
                "service_template.py",
                ".gitignore",
                ".gitattributes",
            }
            expectedFiles = expectedCommittedFiles | {
                "local",
                "ensemble",
                "secrets",
                ".secrets",
            }
            self.assertEqual(set(os.listdir("deploy_dir")), expectedFiles)
            files = set(_path for (_path, _stage) in repo.index.entries)
            expectedCommittedFiles.add("ensemble/ensemble.yaml")
            expectedCommittedFiles.add(".secrets/secrets.yaml")
            expected = {"deploy_dir/" + f for f in expectedCommittedFiles}
            expected.add("README")  # the original file in the repo
            self.assertEqual(files, expected)
            # for n in expectedFiles:
            #     with open("deploy_dir/" + n) as f:
            #         print(n)
            #         print(f.read())

            with open("deploy_dir/ensemble/ensemble.yaml", "w") as f:
                f.write(manifestContent)

            result = runner.invoke(
                cli,
                [
                    "--home",
                    "../unfurl_home",
                    "git",
                    "--dir",
                    "deploy_dir",
                    "commit",
                    "-m",
                    "update manifest",
                    "deploy_dir/ensemble/ensemble.yaml",
                ],
            )
            # uncomment this to see output:
            # print("commit result.output", result.exit_code, result.output)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )

            # "-vvv",
            args = [
                "--home",
                "../unfurl_home",
                "deploy",
                "deploy_dir",
                "--jobexitcode",
                "degraded",
            ]
            result = runner.invoke(cli, args)
            # print("result.output", result.exit_code, result.output)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            self.assertEqual(result.exit_code, 0, result)

    def test_split_repos(self):
        """
        test that the init cli command sets git repos correctly in "polyrepo" mode.
        """
        self.maxDiff = None
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli, ["--home", "", "init", "--use-environment", "test"]
            )
            # uncomment this to see output:
            # print("result.output", result.exit_code, result.output)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            self.assertEqual(result.exit_code, 0, result)

            result = runner.invoke(cli, ["--home", "", "git", "ls-files"])
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            self.assertEqual(result.exit_code, 0, result)
            output = """\
*** Running 'git ls-files' in '.'
.gitattributes
.gitignore
.unfurl-local-template.yaml
ensemble-template.yaml
service_template.py
unfurl.yaml

*** Running 'git ls-files' in './ensemble'
.gitattributes
.gitignore
.secrets/secrets.yaml
.unfurl-local-template.yaml
ensemble.yaml
unfurl.yaml
"""
            self.assertEqual(
                output.strip(), result.output.strip(), result.output.strip()
            )

            with open(".git/info/exclude") as f:
                contents = f.read()
                self.assertIn("ensemble", contents)

            result = runner.invoke(cli, ["--home", "", "deploy", "--commit"])
            # uncomment this to see output:
            # print("result.output", result.exit_code, result.output)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            self.assertEqual(result.exit_code, 0, result)
            result = runner.invoke(
                cli, ["--home", "", "clone", "ensemble", "cloned-ensemble"]
            )
            # print("result.output", result.exit_code, result.output)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            self.assertEqual(result.exit_code, 0, result)
            assert os.path.isdir("cloned-ensemble")

    def test_home_manifest(self):
        """
        test that we can connect to AWS account
        """
        runner = CliRunner()
        with runner.isolated_filesystem():
            # override home so to avoid interferring with other tests
            result = runner.invoke(
                cli,
                [
                    "--home",
                    "./unfurl_home",
                    "init",
                    "--mono",
                    "--skeleton=aws",
                ],
            )
            # uncomment this to see output:
            # print("result.output", result.exit_code, result.output)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            assert os.path.isdir("./unfurl_home"), "home project not created"
            assert os.path.isfile("./unfurl_home/unfurl.yaml"), (
                "home unfurl.yaml not created"
            )

            with open("ensemble/ensemble.yaml", "w") as f:
                f.write(awsTestManifest)

            result = runner.invoke(
                cli,
                [
                    "--home",
                    "./unfurl_home",
                    "git",
                    "commit",
                    "-m",
                    "update manifest",
                    "ensemble/ensemble.yaml",
                ],
            )
            # uncomment this to see output:
            # print("commit result.output", result.exit_code, result.output)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )

            args = [
                #  "-vvv",
                "--home",
                "./unfurl_home",
                "check",
                "--dirty=ok",
                "--commit",
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
            attrs = job.rootResource.find_resource("testNode").attributes
            access_key = attrs["access_key"]
            self.assertEqual(access_key, "mockAWS_ACCESS_KEY_ID")
            access_key2 = attrs["access_key2"]
            self.assertEqual(access_key2, "mockAWS_ACCESS_KEY_ID", access_key2)
            access_key3 = attrs["access_key3"]
            self.assertEqual(access_key3, "mockAWS_ACCESS_KEY_ID", access_key3)

            # check that these are the only recorded changes
            expected = {
                "::testNode": {
                    "access_key": "mockAWS_ACCESS_KEY_ID",
                    "access_key2": "mockAWS_ACCESS_KEY_ID",
                    "access_key3": "mockAWS_ACCESS_KEY_ID",
                }
            }
            changes = job.manifest.manifest.config["changes"][0]["changes"]
            self.assertEqual(expected, changes, changes)

            # changeLogPath = (
            #     "ensemble/" + job.manifest.manifest.config["lastJob"]["changes"]
            # )
            # with open(changeLogPath) as f:
            #     print(f.read())

            # tasks = list(job.workDone.values())
            # print("task", tasks[0].summary())
            # print("job", job.stats(), job.getOutputs())
            # self.assertEqual(job.status.name, "ok")
            # self.assertEqual(job.stats()["ok"], 1)
            # self.assertEqual(job.getOutputs()["aOutput"], "set")

    def test_repo_urls(self):
        urls = {
            "foo/file": None,
            "git@github.com:onecommons/unfurl_site.git": (
                "git@github.com:onecommons/unfurl_site.git",
                "",
                "",
            ),
            "git-local://e67559c0bc47e8ed2afb11819fa55ecc29a87c97:spec/unfurl": (
                "git-local://e67559c0bc47e8ed2afb11819fa55ecc29a87c97:spec",
                "unfurl",
                "",
            ),
            "/home/foo/file": None,
            "/home/foo/repo.git": ("/home/foo/repo.git", "", ""),
            "/home/foo/repo.git#branch:unfurl": (
                "/home/foo/repo.git",
                "unfurl",
                "branch",
            ),
            "https://github.com/onecommons/": (
                "https://github.com/onecommons/",
                "",
                "",
            ),
            "foo/repo.git": ("foo/repo.git", "", ""),
            "https://github.com/onecommons/base.git#branch:unfurl": (
                "https://github.com/onecommons/base.git",
                "unfurl",
                "branch",
            ),
            "file:foo/file": None,
            "foo/repo.git#branch:unfurl": ("foo/repo.git", "unfurl", "branch"),
            "https://github.com/onecommons/base.git": (
                "https://github.com/onecommons/base.git",
                "",
                "",
            ),
            "https://github.com/onecommons/base.git#ref": (
                "https://github.com/onecommons/base.git",
                "",
                "ref",
            ),
            "git@github.com:onecommons/unfurl_site.git#rev:unfurl": (
                "git@github.com:onecommons/unfurl_site.git",
                "unfurl",
                "rev",
            ),
            "file:foo/repo.git#branch:unfurl": (
                "file:foo/repo.git",
                "unfurl",
                "branch",
            ),
            "file:foo/repo.git": ("file:foo/repo.git", "", ""),
            "file:foo/repo#": ("file:foo/repo#", "", ""),
            "file:foo/repo#:path": ("file:foo/repo", "path", ""),
        }
        for url, expected in urls.items():
            if expected:
                isurl = expected[0] != "foo/repo.git"
                assert is_url_or_git_path(url), url
                self.assertEqual(split_git_url(url), expected)
            else:
                isurl = url[0] == "/" or ":" in url
                assert not is_url_or_git_path(url), url

            if isurl:
                # relative urls aren't allowed here, skip those
                rv = RepoView(dict(name="", url=url), None)
                self.assertEqual(normalize_git_url(rv.url), normalize_git_url(url))
            else:
                self.assertRaises(URLException, RepoView, dict(name="", url=url), None)

    def test_home_template(self):
        # test creating and deploying the home template
        runner = CliRunner()
        with runner.isolated_filesystem():
            # override home so to avoid interferring with other tests
            result = runner.invoke(
                cli, ["--no-runtime", "--home", "./unfurl_home", "home", "--init"]
            )
            # uncomment this to see output:
            # print("result.output", result.exit_code, result.output)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )

            assert not os.path.exists("./unfurl_home/.tool_versions")
            makeAsdfFixtures("test_asdf")
            os.environ["ASDF_DATA_DIR"] = os.path.abspath("test_asdf")
            args = [
                #  "-vvv",
                "deploy",
                "./unfurl_home",
                "--jobexitcode",
                "degraded",
            ]
            result = runner.invoke(cli, args)
            # print("result.output", result.exit_code, result.output)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            self.assertEqual(result.exit_code, 0, result)

            # we use this ensemble because it is in a repository with a non-local origin set:
            project = os.path.join(
                os.path.dirname(__file__), "examples/import/testimport-ensemble.yaml"
            )
            result = runner.invoke(
                cli, ["--home", "./unfurl_home", "home", "--register", project]
            )
            # print("result.output", result.exit_code, result.output)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            self.assertEqual(result.exit_code, 0, result)

            # XXX replace above command with deploying an ensemble with an implementation artifact
            #     that will trigger asdf to be installed so we can re-enable testing it:
            # assert os.path.exists("unfurl_home/.tool-versions")
            # assert LocalEnv("unfurl_home").get_manifest()
            # paths = os.environ["PATH"].split(os.pathsep)
            # assert len(paths) >= len(installDirs)
            # for dirs, path in zip(installDirs, paths):
            #     self.assertIn(os.sep.join(dirs), path)

            # assert added to projects
            basedir = os.path.dirname(os.path.dirname(__file__))
            repo = GitRepo(Repo(basedir))
            gitUrl = normalize_git_url(repo.url) + "#:tests/examples"
            # assert added to projects
            # travis-ci does a shallow clone so it doesn't have the initial initial revision
            initial = repo.get_initial_revision()
            with open("./unfurl_home/unfurl.yaml") as f:
                contents = f.read()
                # print("home:\n", contents)
                for line in [
                    "examples:",
                    "url: " + gitUrl,
                    "initial: " + initial,
                ]:
                    self.assertIn(line, contents), line

            # assert added to localRepositories
            with open("./unfurl_home/local/unfurl.yaml") as f:
                contents = f.read()
                # print("local:\n", contents)
                for line in [
                    "url: " + normalize_git_url(gitUrl),
                    "initial: " + initial,
                ]:
                    self.assertIn(line, contents)
                self.assertNotIn("origin:", contents)
            rmtree(os.path.join(os.path.dirname(project), "jobs"))

            externalProjectManifest = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
environment:
 external:
  test:
    manifest:
      file: import/testimport-ensemble.yaml
      project: examples
spec:
  service_template:
    topology_template:
      node_templates:
        testNode:
          type: tosca.nodes.Root
          properties:
            externalEnsemble:
              eval:
                external: test
"""
            with open("externalproject.yaml", "w") as f:
                f.write(externalProjectManifest)

            result = runner.invoke(
                cli,
                [
                    "--home",
                    "./unfurl_home",
                    "plan",
                    "--starttime=1",
                    "externalproject.yaml",
                ],
            )
            # print("result.output", result.exit_code, result.output)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            self.assertEqual(result.exit_code, 0, result)

            # assert that we loaded the external ensemble from the test "examples" project
            # and we're able to reference its outputs
            assert _latestJobs
            testNode = _latestJobs[-1].rootResource.find_resource("testNode")
            assert testNode and testNode.attributes["externalEnsemble"]
            externalEnsemble = testNode.attributes["externalEnsemble"]
            assert "aOutput" in externalEnsemble.attributes["outputs"]
            # make sure we loaded it from the source (not a local checkout)
            assert externalEnsemble.base_dir.startswith(os.path.dirname(__file__))

    def test_remote_git_repo(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["init", "--mono"])
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            with open("ensemble/ensemble.yaml", "w") as f:
                f.write(repoManifestContent)
            ensemble = LocalEnv().get_manifest()
            # Updated origin/master to a319ac1914862b8ded469d3b53f9e72c65ba4b7f
            ensemble.commit("test", True)
            assert ensemble.repo
            assert not ensemble.repo.is_dirty()
            rev = ensemble.repo.revision
            ensemble.repo.reset()
            assert rev != ensemble.repo.revision
            with open("ensemble/ensemble.yaml") as f:
                assert repoManifestContent != f.read()

            path = "base-payments"
            self.assertEqual(
                os.path.abspath(path),
                ensemble.rootResource.find_resource("my_server").attributes[
                    "repo_path"
                ],
            )
            assert os.path.isdir(os.path.join(path, ".git"))
            repo = GitRepo(Repo(path))
            repo.set_url_credentials("a", "pw", True)
            lines = repo.run_cmd(["remote", "-v"])[1].split("\n")
            assert "(fetch)" in lines[0] and "a:pw" in lines[0], lines[0]  # fetch url
            assert "(push)" in lines[1] and "a:pw@" not in lines[1], lines[
                1
            ]  # push url
            assert repo.push_url == repo.repo.git.remote(
                "get-url", "--push", repo.remote.name
            )

    # XXX renable on CI -- this test has gotten really flaky on github actions for some reason
    @unittest.skipIf(os.getenv("CI"), reason="skipping due to flaky CI")
    def test_submodules(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            # override home so to avoid interferring with other tests
            result = runner.invoke(
                cli, ["--home", "./unfurl_home", "init", "test", "--submodule"]
            )
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            self.assertEqual(result.exit_code, 0, result)

            result = runner.invoke(cli, ["clone", "file:test#:", "cloned"])
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            self.assertEqual(result.exit_code, 0, result)
            assert os.path.isfile("cloned/ensemble/.git") and not os.path.isdir(
                "cloned/ensemble/.git"
            )
            assert not os.path.exists("cloned/ensemble1"), result.output


repoManifestContent = """\
  apiVersion: unfurl/v1alpha1
  kind: Ensemble
  spec:
    instances:
      my_server:
        template: my_server
    service_template:
      repositories:
        remote-git-repo:
          # use a remote git repository that is fast to download but big enough to test the fetching progress output
          url: https://github.com/onecommons/base-payments.git
      topology_template:
        node_templates:
          my_server:
            type: tosca.nodes.Compute
            properties:
              repo_path:
                eval:
                  get_dir: remote-git-repo
  """

reifiedManifestContent = """\
  apiVersion: unfurl/v1alpha1
  kind: Ensemble
  spec:
    # test that we can reference a repository declared in the environment during parse-time
    +?include:
      file: missing.yaml
      repository: 
        name: include-early-repo
        url: file:///different-than-env
    instances:
      git-repo:
        template: git-repo
    service_template:
      topology_template:
        node_templates:
          git-repo:
            type: unfurl.nodes.Repository
  """

projectManifest = """\
apiVersion: unfurl/v1alpha1
kind: Project
environments:
  defaults:
    variables:
      git_token: secret
    repositories:
      spec:
        url: https://github.com/onecommons/blueprints/example.git
        credential:
          user: deploy-token
          token:
            get_env: git_token
      git-repo:
        url: https://github.com/onecommons/base-payments.git
        revision: 8454bc
      include-early-repo:
        url: file:///nowhere
"""


def test_reified_repo(caplog):
    runner = CliRunner()
    with runner.isolated_filesystem():
        with open("unfurl.yaml", "w") as f:
            f.write(projectManifest)
        os.mkdir("ensemble")
        with open("ensemble/ensemble.yaml", "w") as f:
            f.write(reifiedManifestContent)
        manifest = LocalEnv().get_manifest()
        # .repository works on reified instances:
        repository = manifest.repositories.get("git-repo")
        assert isinstance(repository, RepoView), repository
        assert (
            manifest.rootResource.query("::git-repo::.repository::revision") == "8454bc"
        )
        # test that credentials for repository are rewrite urls and evaluate env vars
        # and make sure the environment can override the built-in "spec" repository
        assert (
            manifest.repositories.get("spec").url
            == "https://deploy-token:secret@github.com/onecommons/blueprints/example.git"
        )
        assert (
            'skipping inline repository definition for "include-early-repo", it was previously defined'
            in caplog.text
        )


def test_clone_ensemble_repo():
    runner = CliRunner()
    with runner.isolated_filesystem():
        run_cmd(runner, ["--home", "local_home", "--no-runtime", "home", "--init"])
        run_cmd(
            runner,
            ["--home", "local_home", "init", "--use-environment", "inner", "src"],
        )
        # use a home project so git-local: url resolve across projects
        # we need this because we are doing a remote clone of an ensemble repo with a git-local spec url
        run_cmd(
            runner, ["--home", "local_home", "home", "--register", "src"]
        )
        ensemble_repo_files = set([
            "unfurl.yaml",
            ".gitignore",
            ".gitattributes",
            ".git",
            "ensemble.yaml",
            ".secrets",
            ".unfurl-local-template.yaml",
        ])
        assert set(os.listdir("src/ensemble")) == (
            ensemble_repo_files | set(["local", "secrets"])
        )
        run_cmd(
            runner,
            ["--home", "local_home", "init", "--use-environment", "outer", "dst"],
        )
        # use URL with fragment instead of file path to induce remote cloning semantics
        run_cmd(runner, ["--home", "local_home", "clone", "file:src/ensemble#:", "dst"])
        assert "ensemble1" in os.listdir("dst")
        assert set(os.listdir("dst/ensemble1")) == ensemble_repo_files
        local_env = LocalEnv("dst/ensemble1")
        assert local_env.manifest_context_name == "inner"
        assert local_env.project.projectRoot.endswith("dst")
        local_env2 = LocalEnv("dst")
        assert local_env2.manifest_context_name == "outer"
        # print_config("dst/ensemble1")
        run_cmd(runner, ["--home", "local_home", "deploy", "dst/ensemble1"])


skeletons_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "unfurl", "skeletons")
skeletons = [
    p.name
    for p in os.scandir(skeletons_dir)
    if p.is_dir()
]


@pytest.mark.parametrize("skeleton", skeletons)
def test_skeletons(skeleton):
    runner = CliRunner()
    with runner.isolated_filesystem():
        run_cmd(runner, ["init", "--skeleton", skeleton, "--use-environment", "test", skeleton, "myensemble"])
        run_cmd(runner, ["validate", skeleton])
