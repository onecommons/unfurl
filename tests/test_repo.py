import unittest
import os
import traceback
import pytest
from urllib.parse import quote
from click.testing import CliRunner
from unfurl.__main__ import cli, _latestJobs
from unfurl.localenv import LocalEnv
from unfurl.projectpaths import rmtree
from unfurl.repo import (
    split_git_url,
    split_git_url_with_commit,
    is_url_or_git_path,
    make_actor,
    RepoView,
    normalize_git_url,
    normalize_git_url_hard,
    GitRepo,
)
from git import Repo
from unfurl.configurator import Configurator, Status
from toscaparser.common.exception import URLException
from unfurl.testing import run_cmd
from unfurl.util import split_url_fragment
from unfurl.yamlmanifest import YamlManifest
from .utils import print_config

SAVE_TMP = os.getenv("UNFURL_TEST_TMPDIR")

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
                ":::testNode": {
                    "access_key": "mockAWS_ACCESS_KEY_ID",
                    "access_key2": "mockAWS_ACCESS_KEY_ID",
                    "access_key3": "mockAWS_ACCESS_KEY_ID",
                }
            }
            changes = job.manifest.manifest.config["changes"][0]["changes"]
            assert expected == changes

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
        url="https://gitlab.com/onecommons/kubernetes-deployment-kicker.git#:.unfurl"
        r = RepoView(dict(name="",url=url), None)
        assert r.url == url
        assert r.url == r.as_git_url()

        url="git-local://5e661139cd335cf557c087ac18afaea24d320732:/csar_wordpress_valid_artifact_multi"
        r = RepoView(dict(name="",url=url), None)
        assert r.url == r.as_git_url()

        url="git-local://5e661139cd335cf557c087ac18afaea24d320732#:csar_wordpress_valid_artifact_multi"
        r = RepoView(dict(name="",url=url), None)
        assert r.url == url
        assert r.url == r.as_git_url()

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
                if not rv.url.startswith("file:"):
                    assert rv.url.strip("#:") == rv.as_git_url().strip("#:"), url
            else:
                self.assertRaises(URLException, RepoView, dict(name="", url=url), None)

    def test_split_git_url_with_commit(self):
        cases = {
            "https://example.com/repo.git#v1.0.0": ("https://example.com/repo.git", "", "v1.0.0", ""),
            "https://example.com/repo.git#~abc123": ("https://example.com/repo.git", "", "", "abc123"),
            "https://example.com/repo.git#v1.0.0~abc123": ("https://example.com/repo.git", "", "v1.0.0", "abc123"),
            "https://example.com/repo.git#v1.0.0~abc123:src/main.py": ("https://example.com/repo.git", "src/main.py", "v1.0.0", "abc123"),
            "https://example.com/repo.git#~abc123:src/main.py": ("https://example.com/repo.git", "src/main.py", "", "abc123"),
        }
        for url, expected in cases.items():
            self.assertEqual(split_git_url_with_commit(url), expected, url)

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

    def test_submodules(self):
        runner = CliRunner()
        # Since CVE-2022-39253 (git 2.38.1), `git clone --recurse-submodules` refuses
        # the file:// transport by default. Override via env so we don't touch the
        # user's global git config: GIT_CONFIG_COUNT/KEY_N/VALUE_N layers config onto
        # every git subprocess without modifying any config file.
        git_env = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "protocol.file.allow",
            "GIT_CONFIG_VALUE_0": "always",
        }
        prev_env = {k: os.environ.get(k) for k in git_env}
        os.environ.update(git_env)
        try:
            with runner.isolated_filesystem(
                os.getenv("UNFURL_TEST_TMPDIR")
            ) as tmp_dir:
                print("saving to", tmp_dir)
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
        finally:
            for k, v in prev_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


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
        url: file:nowhere
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
    with runner.isolated_filesystem(SAVE_TMP) as test_dir:
        if SAVE_TMP:
            print("saving to", test_dir)

        run_cmd(runner, ["--home", "local_home", "--no-runtime", "home", "--init"])
        skeletons_vars = "--var vaultid e1 --var VAULT_PASSWORD uvdAr58vO0ZHo7".split()
        run_cmd(
            runner,
            ["--home", "local_home", "init", "--use-environment", "inner", "src"]
            + skeletons_vars,
        )
        # use a home project so git-local: url resolve across projects
        # we need this because we are doing a remote clone of an ensemble repo with a git-local spec url
        run_cmd(
            runner, ["--home", "local_home", "home", "--register", "src"]
        )
        ensemble_repo_files = set(
            [
                "unfurl.yaml",
                ".gitignore",
                ".gitattributes",
                ".git",
                "ensemble.yaml",
                ".secrets",
                ".unfurl-local-template.yaml",
                "local",
            ]
        )
        assert set(os.listdir("src/ensemble")) == (
            ensemble_repo_files | set(["secrets"])
        )
        run_cmd(
            runner,
            ["--home", "local_home", "init", "--use-environment", "outer", "dst"],
        )
        # use URL with fragment instead of file path to induce remote cloning semantics
        run_cmd(
            runner,
            ["--home", "local_home", "clone", "file:src/ensemble#:", "dst"]
            + skeletons_vars,
        )
        assert "ensemble1" in os.listdir("dst")
        assert set(os.listdir("dst/ensemble1")) == ensemble_repo_files
        local_env = LocalEnv("dst/ensemble1")
        assert local_env.manifest_environment_name == "inner"
        assert local_env.project.projectRoot.endswith("dst")
        local_env2 = LocalEnv("dst")
        assert local_env2.manifest_environment_name == "outer"
        # print_config("dst/ensemble1")
        run_cmd(runner, ["--home", "local_home", "deploy", "dst/ensemble1"])
        # secrets folder should be decrypted now
        assert "secrets" in os.listdir("dst/ensemble1")
        # test that unfurl commit updates repository urls:
        remote_url = "ssh://git@unfurl.cloud/remote/blueprint.git"
        os.system("git -C src remote add origin git@unfurl.cloud:remote/blueprint.git")
        run_cmd(
            runner,
            [
                "--home",
                "local_home",
                "commit",
                "--update-repositories-only",
                "dst/ensemble1",
            ],
        )
        with open("dst/ensemble1/ensemble.yaml") as f:
            content = f.read()
            assert remote_url in content, content


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


# --- commit authorship -------------------------------------------------------


@pytest.fixture
def author_env(monkeypatch):
    """Pin the ambient git identity so author/committer defaults are predictable.

    ``git.Actor.author()`` consults ``GIT_AUTHOR_*`` before falling back to
    ``user.name`` / ``user.email``, so the tests set the env explicitly rather than
    relying on whatever the developer's or CI runner's git config happens to be.
    """
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Default Author")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "default-author@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Server Bot")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "bot@server.local")


def _new_repo(path) -> GitRepo:
    os.makedirs(path, exist_ok=True)
    return GitRepo(Repo.init(path))


def _write(repo: GitRepo, name: str, contents: str) -> str:
    path = os.path.join(repo.working_dir, name)
    with open(path, "w") as f:
        f.write(contents)
    return path


@pytest.mark.parametrize(
    "actor,expected",
    [
        # fully specified: used verbatim
        ("Jo Tester <jo@example.com>", ("Jo Tester", "jo@example.com")),
        # bare name / bare email: the missing half comes from GIT_AUTHOR_*
        ("Jo Tester", ("Jo Tester", "default-author@example.com")),
        ("jo@example.com", ("Default Author", "jo@example.com")),
        ("<jo@example.com>", ("Default Author", "jo@example.com")),
        # a name with spaces isn't mistaken for an email address
        ("Jo Q. Tester", ("Jo Q. Tester", "default-author@example.com")),
    ],
)
def test_make_actor(author_env, actor, expected):
    made = make_actor(actor, None, "author")
    assert made is not None
    assert (made.name, made.email) == expected


@pytest.mark.parametrize("actor", [None, "", "   "])
def test_make_actor_empty(author_env, actor):
    # no actor -> None, so git applies its own configured identity
    assert make_actor(actor) is None


def test_make_actor_role(author_env):
    # `role` selects which of GIT_AUTHOR_* / GIT_COMMITTER_* fills a missing half
    assert make_actor("Jo Tester", None, "author").email == "default-author@example.com"
    assert make_actor("Jo Tester", None, "committer").email == "bot@server.local"
    # the default role is "committer"
    assert make_actor("Jo Tester").email == "bot@server.local"


def test_commit_files_author(author_env, tmp_path):
    repo = _new_repo(tmp_path / "repo")
    path = _write(repo, "a.txt", "one")
    commit = repo.commit_files([path], "with author", "Jo Tester <jo@example.com>")
    assert (commit.author.name, commit.author.email) == ("Jo Tester", "jo@example.com")
    # only the author is overridden; the committer stays the ambient identity
    assert (commit.committer.name, commit.committer.email) == (
        "Server Bot",
        "bot@server.local",
    )

    # no author -> git's configured author
    path = _write(repo, "a.txt", "two")
    commit = repo.commit_files([path], "no author")
    assert (commit.author.name, commit.author.email) == (
        "Default Author",
        "default-author@example.com",
    )


def test_commit_author(author_env, tmp_path):
    repo = _new_repo(tmp_path / "repo")
    repo.repo.index.add([_write(repo, "a.txt", "one")])
    commit = repo.commit("first", "Jo Tester <jo@example.com>")
    assert commit is not None
    assert (commit.author.name, commit.author.email) == ("Jo Tester", "jo@example.com")
    assert commit.committer.name == "Server Bot"


def test_commit_unborn_head(author_env, tmp_path):
    """The first commit has no HEAD to diff against."""
    repo = _new_repo(tmp_path / "repo")
    assert not repo.repo.head.is_valid()
    # nothing staged -> no empty root commit
    assert repo.commit("nothing staged") is None
    assert not repo.repo.head.is_valid()
    # staged -> the root commit is created
    repo.repo.index.add([_write(repo, "a.txt", "one")])
    assert repo.commit("root commit") is not None
    assert repo.revision


def test_commit_skipped_when_unchanged(author_env, tmp_path):
    repo = _new_repo(tmp_path / "repo")
    repo.commit_files([_write(repo, "a.txt", "one")], "first")
    revision = repo.revision
    # index matches HEAD, so there is nothing to commit
    assert repo.commit("no-op") is None
    assert repo.revision == revision
    # ... and RepoView reports that nothing was committed
    view = RepoView({"name": "test", "url": repo.working_dir}, repo)
    assert view.commit("no-op") == 0


def test_init_commit_author(author_env, tmp_path):
    """`author` passed to create_project/clone reaches every commit they make."""
    from unfurl import init

    project = str(tmp_path / "project")
    init.create_project(
        project, home="", no_runtime=True, author="Jo Tester <jo@example.com>"
    )
    init.clone(
        project,
        project,
        "ensemble2",
        existing=True,
        mono=True,
        home="",
        author="Ada Lovelace <ada@example.com>",
    )
    repo = GitRepo(Repo(project))
    authors = [
        (c.author.name, c.author.email, c.committer.name)
        for c in repo.repo.iter_commits()
    ]
    # newest first: clone's commits, then create_project's
    assert authors[-2:] == [
        ("Jo Tester", "jo@example.com", "Server Bot"),
        ("Jo Tester", "jo@example.com", "Server Bot"),
    ]
    assert all(a[:2] == ("Ada Lovelace", "ada@example.com") for a in authors[:-2])
    # the initial commit is still tagged, i.e. it wasn't skipped
    assert "INITIAL" in [t.name for t in repo.repo.tags]


def test_uri_template_git_urls():
    """A "#" inside an expression is part of it, not the start of the fragment."""
    assert split_url_fragment("git://a.com/x.git{#ref}") == (
        "git://a.com/x.git{#ref}",
        "",
    )
    assert split_url_fragment("git://a.com/x.git#{+ref}:src") == (
        "git://a.com/x.git",
        "{+ref}:src",
    )
    # unchanged for urls without templates
    assert split_url_fragment("git://a.com/x.git#v1:src") == (
        "git://a.com/x.git",
        "v1:src",
    )
    assert split_git_url("git://a.com/x.git{#ref}") == (
        "git://a.com/x.git{#ref}",
        "",
        "",
    )
    assert split_git_url("git://a.com/x.git#{+ref}:src/{name}") == (
        "git://a.com/x.git",
        "src/{name}",
        "{+ref}",
    )
    assert split_git_url_with_commit("git://a.com/x.git#main~abc123:src") == (
        "git://a.com/x.git",
        "src",
        "main",
        "abc123",
    )


SELF_REFERENCING_ENSEMBLE = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
spec:
  service_template:
    repositories:
      bar:
        url: https://example.com/foo/bar.git
        revision: main
    imports:
      - file: types.yaml
        repository: bar
    topology_template:
      node_templates:
        n1:
          type: My.Node
"""

SELF_REFERENCING_TYPES = """
tosca_definitions_version: tosca_simple_unfurl_1_0_0
node_types:
  My.Node:
    derived_from: tosca.nodes.Root
"""


def test_repository_referencing_its_own_repo(tmp_path):
    """A repository can name the repo the ensemble already lives in.

    The Python DSL emits exactly that for a cross-module import whenever the
    module was loaded through a repository, so the generated YAML imports from
    the repository it is already inside. That has to resolve without cloning:
    when the ensemble isn't in an Unfurl project -- how the server exports a
    standalone repository -- there is nowhere to clone to.
    """
    repo_dir = tmp_path / "myrepo"
    repo_dir.mkdir()
    (repo_dir / "types.yaml").write_text(SELF_REFERENCING_TYPES)
    (repo_dir / "ensemble.yaml").write_text(SELF_REFERENCING_ENSEMBLE)
    repo = Repo.init(repo_dir)
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "test@example.com")
        cw.set_value("user", "name", "test")
    repo.create_remote("origin", "https://example.com/foo/bar.git")
    repo.git.add(A=True)
    repo.git.commit("-m", "init")

    # UNFURL_SEARCH_ROOT confines the search for unfurl.yaml to the repository,
    # so the ensemble has no project -- the same shape the server's export
    # builds in _make_readonly_localenv().
    local_env = LocalEnv(
        str(repo_dir / "ensemble.yaml"),
        homePath="",
        overrides={"UNFURL_SEARCH_ROOT": str(repo_dir)},
    )
    # no project and no home project means nowhere to clone to
    assert local_env.project is None
    assert local_env.homeProject is None
    manifest = local_env.get_manifest(skip_validation=True)

    resolved = manifest.repositories["bar"].repo
    assert resolved, "repository 'bar' was not resolved to the containing repo"
    assert os.path.normpath(resolved.working_dir) == os.path.normpath(str(repo_dir))
    # the import actually loaded
    assert "My.Node" in manifest.tosca.template.topology_template.custom_defs


@pytest.mark.parametrize(
    "url,expected",
    [
        # Every spelling of one repository folds to one identity. This is
        # what makes it usable as a key: the same repo cloned over https by
        # one user and ssh by another must not read as two.
        ("https://unfurl.cloud/onecommons/cloudmap.git", "unfurl.cloud/onecommons/cloudmap"),
        ("https://unfurl.cloud/onecommons/cloudmap", "unfurl.cloud/onecommons/cloudmap"),
        ("https://unfurl.cloud/onecommons/cloudmap/", "unfurl.cloud/onecommons/cloudmap"),
        ("ssh://git@unfurl.cloud/onecommons/cloudmap.git", "unfurl.cloud/onecommons/cloudmap"),
        # scp-style, which git accepts and no URL parser handles natively
        ("git@unfurl.cloud:onecommons/cloudmap.git", "unfurl.cloud/onecommons/cloudmap"),
        ("git://unfurl.cloud/onecommons/cloudmap.git", "unfurl.cloud/onecommons/cloudmap"),
        # credentials are dropped, so a URL with a token in it still matches
        (
            "https://user:pass@unfurl.cloud/onecommons/cloudmap.git",
            "unfurl.cloud/onecommons/cloudmap",
        ),
        ("https://unfurl.cloud/onecommons/cloudmap.git#main:sub/dir", "unfurl.cloud/onecommons/cloudmap"),
        # a non-default port distinguishes hosts and is kept
        ("https://unfurl.cloud:8443/onecommons/cloudmap.git", "unfurl.cloud:8443/onecommons/cloudmap"),
        # DNS is case-insensitive, so the host folds...
        ("https://UNFURL.cloud/onecommons/cloudmap.git", "unfurl.cloud/onecommons/cloudmap"),
        ("HTTPS://UNFURL.CLOUD/onecommons/cloudmap.git", "unfurl.cloud/onecommons/cloudmap"),
        # ...but the path does not: a case-sensitive backend can serve
        # these as different repositories, and merging two repos under one
        # identity is worse than failing to merge one.
        ("https://unfurl.cloud/OneCommons/CloudMap.git", "unfurl.cloud/OneCommons/CloudMap"),
        # local paths pass through
        ("/tmp/local/repo", "/tmp/local/repo"),
        ("file:///tmp/local/repo", "/tmp/local/repo"),
        ("", ""),
    ],
)
def test_normalize_git_url_hard(url, expected):
    assert normalize_git_url_hard(url) == expected
    # Idempotent: feeding the result back in must not change it, or a
    # value normalized twice would stop matching one normalized once.
    assert normalize_git_url_hard(normalize_git_url_hard(url)) == expected


def test_normalize_git_url_case_folding_scope():
    # hard=1 keeps the user name (case-sensitive) while still folding the host.
    assert normalize_git_url("https://User:pw@HOST/a/b.git", hard=1) == "https://User@host/a/b.git"
    # hard=0 is untouched -- callers using it want the URL as given.
    assert normalize_git_url("https://HOST/A/b.git") == "https://HOST/A/b.git"


@pytest.mark.parametrize("remote", ["", "https://example.com/org/outer.git"])
def test_saving_keeps_a_nested_projects_spec_url(tmp_path, monkeypatch, remote):
    """A project inside a larger git repo still loads after its manifest is
    saved: the save doesn't drop the project's folder from the ``spec``
    repository's url."""
    outer = tmp_path / "outer"
    (outer / "sub").mkdir(parents=True)
    repo = Repo.init(outer)
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "test@example.com")
        cw.set_value("user", "name", "test")
    (outer / "README").write_text("readme")
    repo.git.add(A=True)
    repo.git.commit("-m", "init")
    monkeypatch.chdir(outer / "sub")
    run_cmd(CliRunner(), ["--home", "", "init", ".", "--existing"])
    if remote:
        # added later, which is what updating the url is for
        repo.create_remote("origin", remote)

    ensemble_path = str(outer / "sub" / "ensemble" / "ensemble.yaml")
    manifest = LocalEnv(ensemble_path, homePath="").get_manifest()
    manifest.update_repositories()  # as saving a job does
    manifest.manifest.save()

    reloaded = LocalEnv(ensemble_path, homePath="").get_manifest()
    spec_url = reloaded.repositories["spec"].url
    if remote:
        assert spec_url == remote + "#:sub"
    else:
        assert spec_url.startswith("git-local://") and spec_url.endswith("/sub")


def test_add_transient_credentials_rewrites_the_url():
    import git as gitpython
    from unfurl.repo import add_transient_credentials

    url = "https://gitlab.example.com/org/repo.git"
    cmd = gitpython.Git()
    add_transient_credentials(cmd, url, "deploy", "t=k@n:x/y")
    # applies to the next command only
    assert (
        cmd.ls_remote("--get-url", url)
        == "https://deploy:t%3Dk%40n%3Ax%2Fy@gitlab.example.com/org/repo.git"
    )
    assert cmd.ls_remote("--get-url", url) == url


USER, TOKEN = "deploy", "t=k@n:x/y"


class AuthGitServer:
    """Bare repositories served by `git http-backend` behind basic auth for
    USER and TOKEN."""

    def __init__(self, root, port):
        self.root = root
        self.port = port

    def url(self, name, credentials=False):
        userinfo = f"{USER}:{quote(TOKEN, safe='')}@" if credentials else ""
        return f"http://{userinfo}127.0.0.1:{self.port}/{name}.git"

    def commit(self, name, files, symlinks={}):
        """Commit ``files`` (path: text) and ``symlinks`` (path: target) to
        repository ``name``, creating it."""
        work = self.root / "work" / name
        if not work.exists():
            work.mkdir(parents=True)
            _git("init", "-q", "-b", "main", cwd=work)
        for path, text in files.items():
            (work / path).write_text(text)
        for path, target in symlinks.items():
            os.symlink(target, work / path)
        _git("add", "-A", cwd=work)
        _git("commit", "-q", "-m", "commit", cwd=work)
        bare = self.root / "served" / f"{name}.git"
        if not bare.exists():
            _git("clone", "-q", "--bare", str(work), str(bare), cwd=self.root)
        else:
            _git("push", "-q", str(bare), "main", cwd=work)


def _git(*args, cwd):
    import subprocess

    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def auth_git_server(tmp_path, monkeypatch):
    """An AuthGitServer with repository "repo" (a README). Git is isolated
    from this machine's config, so nothing prompts or stores the token."""
    import base64
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.delenv("GIT_ASKPASS", raising=False)
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    # left set by tests that run the cli, where it would skip the pulls tested
    monkeypatch.delenv("UNFURL_SKIP_UPSTREAM_CHECK", raising=False)
    (tmp_path / "served").mkdir()
    expected = "Basic " + base64.b64encode(f"{USER}:{TOKEN}".encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        def backend(self):
            if self.headers.get("Authorization") != expected:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="git"')
                self.end_headers()
                return
            path, _, query = self.path.partition("?")
            length = int(self.headers.get("Content-Length") or 0)
            env = dict(
                os.environ,
                GIT_PROJECT_ROOT=str(tmp_path / "served"),
                GIT_HTTP_EXPORT_ALL="1",
                PATH_INFO=path,
                QUERY_STRING=query,
                REQUEST_METHOD=self.command,
                CONTENT_TYPE=self.headers.get("Content-Type", ""),
                HTTP_CONTENT_ENCODING=self.headers.get("Content-Encoding", ""),
                HTTP_GIT_PROTOCOL=self.headers.get("Git-Protocol", ""),
                REMOTE_USER=USER,
            )
            out = subprocess.run(
                ["git", "http-backend"],
                input=self.rfile.read(length),
                env=env,
                capture_output=True,
            ).stdout
            sep = b"\r\n\r\n" if b"\r\n\r\n" in out else b"\n\n"
            head, _, body = out.partition(sep)
            status, headers = 200, []
            for line in head.decode().splitlines():
                key, _, value = line.partition(":")
                if key.lower() == "status":
                    status = int(value.split()[0])
                else:
                    headers.append((key, value.strip()))
            self.send_response(status)
            for key, value in headers:
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = backend

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    git_server = AuthGitServer(tmp_path, server.server_address[1])
    git_server.commit("repo", {"README": "hi\n"})
    yield git_server
    server.shutdown()


def test_a_credentialed_clone_stores_no_credentials(auth_git_server, tmp_path):
    from unfurl.repo import Repo as UnfurlRepo
    from unfurl.util import UnfurlError

    url = auth_git_server.url("repo")
    with pytest.raises(UnfurlError):
        UnfurlRepo.create_working_dir(url, str(tmp_path / "anon"))

    for symlinks in (True, False):
        dest = tmp_path / f"clone-{symlinks}"
        repo = UnfurlRepo.create_working_dir(
            url, str(dest), username=USER, password=TOKEN, symlinks=symlinks
        )
        assert (dest / "README").exists()
        assert repo.repo.git.config("remote.origin.url") == url
        config = (dest / ".git" / "config").read_text()
        assert "t%3Dk" not in config and USER not in config
        if not symlinks:
            assert repo.repo.git.config("core.symlinks") == "false"
        # nor does the repository object the clone returns, which is cached
        # and used for later requests
        fetched = repo.run_cmd(["fetch", "-q"])[0] == 0
        assert not fetched, f"fetched with the clone's credentials ({symlinks=})"


def test_git_config_env_appends():
    from unfurl.repo import git_config_env

    inherited = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "a.b"}
    assert git_config_env([("c.d", "1"), ("e.f", "2")], inherited) == {
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_1": "c.d",
        "GIT_CONFIG_VALUE_1": "1",
        "GIT_CONFIG_KEY_2": "e.f",
        "GIT_CONFIG_VALUE_2": "2",
    }
    assert git_config_env([("c.d", "1")], {})["GIT_CONFIG_KEY_0"] == "c.d"


MODES = {
    "hosted": dict(apply_url_credentials=True, transient_url_credentials=True),
    "gui": dict(apply_url_credentials=True),
    "cli": {},
}


def _project_env(tmp_path, mode="hosted", **overrides):
    """A LocalEnv on an empty project, with the overrides of ``mode``:
    the hosted server's, the local gui's, or the command line's, and
    ``overrides``."""
    from unfurl.localenv import LocalEnv

    project = tmp_path / "project"
    project.mkdir()
    _git("init", "-q", cwd=project)
    (project / "unfurl.yaml").write_text("apiVersion: unfurl/v1.0.0\nkind: Project\n")
    return LocalEnv(
        str(project), homePath="", can_be_empty=True, overrides={**MODES[mode], **overrides}
    )


@pytest.mark.parametrize("mode", MODES)
def test_hosted_server_clones_keep_no_credentials(auth_git_server, tmp_path, mode):
    env = _project_env(tmp_path, mode)
    given = auth_git_server.url("repo", credentials=True)
    repo, _, created = env.find_or_create_working_dir(given)
    assert created
    stored = repo.repo.git.config("remote.origin.url")
    # on a user's own machine the url is kept as given, as git does
    assert stored == (auth_git_server.url("repo") if mode == "hosted" else given)


def test_the_server_clones_and_pulls_with_the_requests_credentials(
    auth_git_server, tmp_path
):
    """The server's clones on the cloud server's host are made and pulled
    with the request's credentials, which none of them stores."""
    from unfurl.repo import request_credentials

    env = _project_env(tmp_path)
    host = f"http://127.0.0.1:{auth_git_server.port}/"
    url = auth_git_server.url("repo")
    token = request_credentials.set((host, USER, TOKEN))
    try:
        repo, _, created = env.find_or_create_working_dir(url)
        assert created and repo.repo.git.config("remote.origin.url") == url
        auth_git_server.commit("repo", {"README": "updated\n"})
        repo, _, created = env.find_or_create_working_dir(url)
        assert not created
    finally:
        request_credentials.reset(token)
    assert open(os.path.join(repo.working_dir, "README")).read() == "updated\n"


def test_the_server_borrows_no_clones_stored_credentials(auth_git_server, tmp_path):
    """Without the request's credentials, the server doesn't use those stored
    in another clone's url, which could be another user's."""
    import git as gitpython
    from unfurl.util import UnfurlError

    env = _project_env(tmp_path)
    auth_git_server.commit("project", {"unfurl.yaml": "x\n"})
    keeper = tmp_path / "project" / "keeper"
    given = auth_git_server.url("project", credentials=True)
    _git("clone", "-q", given, str(keeper), cwd=tmp_path)
    env.project.workingDirs[str(keeper)] = GitRepo(gitpython.Repo(keeper)).as_repo_view()
    with pytest.raises(UnfurlError):
        env.find_or_create_working_dir(auth_git_server.url("repo"))


@pytest.mark.parametrize("mode", MODES)
def test_a_blueprint_clone_keeps_no_credentials(auth_git_server, tmp_path, mode):
    from unfurl.init import EnsembleBuilder

    project_yaml = "apiVersion: unfurl/v1.0.0\nkind: Project\n"
    auth_git_server.commit("blueprint", {"unfurl.yaml": project_yaml})
    given = auth_git_server.url("blueprint", credentials=True)
    options: dict = {"home": ""}
    if mode != "cli":
        options["parent_localenv"] = _project_env(tmp_path, mode)
    dest = tmp_path / "dest"
    EnsembleBuilder(given, "ensemble", options).clone_remote_project(None, str(dest))
    stored = gitpython_config(dest, "remote.origin.url")
    assert stored == (auth_git_server.url("blueprint") if mode == "hosted" else given)


@pytest.mark.parametrize("safe_mode", [True, False])
def test_an_untrusted_projects_clones_check_out_no_symlinks(
    auth_git_server, tmp_path, safe_mode
):
    """In safe mode, the server's, a committed symlink could expose a file
    outside the clone: it's written as a plain file, at the first checkout
    and every later one."""
    auth_git_server.commit("links", {"README": "x"}, symlinks={"first": "/etc/hosts"})
    env = _project_env(tmp_path, "cli", safe_mode=safe_mode)
    url = auth_git_server.url("links", credentials=True)
    repo, _, created = env.find_or_create_working_dir(url)
    assert created
    auth_git_server.commit("links", {}, symlinks={"later": "/etc/hosts"})
    env.find_or_create_working_dir(url)
    for name in ["first", "later"]:
        path = os.path.join(repo.working_dir, name)
        assert os.path.lexists(path)
        assert os.path.islink(path) != safe_mode, name



def test_a_local_copy_of_a_clone_without_symlinks_has_none(tmp_path):
    """Copying a working directory, as cloning a local project does, keeps
    its ``core.symlinks=false``."""
    source = tmp_path / "source"
    source.mkdir()
    _git("init", "-q", "-b", "main", cwd=source)
    os.symlink("/etc/hosts", source / "link")
    _git("add", "-A", cwd=source)
    _git("commit", "-q", "-m", "commit", cwd=source)
    for symlinks in (True, False):
        _git("config", "core.symlinks", str(symlinks).lower(), cwd=source)
        copy = GitRepo(Repo(source)).clone(str(tmp_path / f"copy-{symlinks}"))
        assert os.path.islink(os.path.join(copy.working_dir, "link")) == symlinks
        if not symlinks:
            assert copy.repo.git.config("core.symlinks") == "false"

def test_a_clone_made_before_safe_mode_stops_checking_out_symlinks(
    auth_git_server, tmp_path
):
    auth_git_server.commit("links", {"README": "x"})
    env = _project_env(tmp_path, "cli")
    url = auth_git_server.url("links", credentials=True)
    repo, _, _ = env.find_or_create_working_dir(url)
    env.overrides["safe_mode"] = True
    auth_git_server.commit("links", {}, symlinks={"later": "/etc/hosts"})
    env.find_or_create_working_dir(url)
    assert not os.path.islink(os.path.join(repo.working_dir, "later"))


def test_an_untrusted_blueprint_clone_checks_out_no_symlinks(auth_git_server, tmp_path):
    from unfurl.init import EnsembleBuilder

    project_yaml = "apiVersion: unfurl/v1.0.0\nkind: Project\n"
    auth_git_server.commit(
        "blueprint", {"unfurl.yaml": project_yaml}, symlinks={"link": "/etc/hosts"}
    )
    options: dict = {
        "home": "",
        "parent_localenv": _project_env(tmp_path, "hosted", safe_mode=True),
    }
    dest = tmp_path / "dest"
    given = auth_git_server.url("blueprint", credentials=True)
    EnsembleBuilder(given, "ensemble", options).clone_remote_project(None, str(dest))
    assert (dest / "link").exists() and not (dest / "link").is_symlink()


def gitpython_config(path, key):
    import git as gitpython

    return gitpython.Repo(path).git.config(key)


@pytest.mark.parametrize("gui", [False, True])
def test_only_the_hosted_server_keeps_clones_free_of_credentials(
    tmp_path, monkeypatch, gui
):
    from unfurl.server.serve import app, _make_readonly_localenv

    project_env = _project_env(tmp_path, "cli")
    monkeypatch.setitem(app.config, "UNFURL_GUI_MODE", project_env if gui else None)
    monkeypatch.setitem(app.config, "UNFURL_OPTIONS", {})
    with app.app_context():
        err, local_env = _make_readonly_localenv(str(tmp_path), "project")
    assert not err and local_env
    assert local_env.overrides.get("apply_url_credentials")
    assert bool(local_env.overrides.get("transient_url_credentials")) == (not gui)


def _serve(monkeypatch, auth_git_server, tmp_path, gui=False):
    """The server app, with its cloud server at the test git server, in
    server mode or, with ``gui``, the local gui's."""
    from unfurl.server import serve

    config = serve.app.config
    monkeypatch.setitem(
        config, "UNFURL_CLOUD_SERVER", f"http://127.0.0.1:{auth_git_server.port}/"
    )
    # hosted, not `unfurl serve <path>` serving a local project
    monkeypatch.delenv("UNFURL_SERVE_PATH", raising=False)
    monkeypatch.setitem(config, "UNFURL_CLONE_ROOT", str(tmp_path / "clones"))
    monkeypatch.setitem(config, "UNFURL_LOCAL_PROJECTS", {})
    monkeypatch.setitem(
        config, "UNFURL_GUI_MODE", _project_env(tmp_path, "gui") if gui else None
    )
    return serve


def _git_credentials_header():
    from base64 import b64encode

    return {"X-Git-Credentials": b64encode(f"{USER}:{TOKEN}".encode()).decode()}


def _stored_config(repo):
    with open(os.path.join(repo.working_dir, ".git", "config")) as f:
        return f.read()


def test_the_servers_project_clones_keep_no_credentials(
    auth_git_server, tmp_path, monkeypatch
):
    """The server clones and pulls a project with the request's credentials,
    and stores them nowhere; once the request ends, nothing holds them."""
    from unfurl.repo import request_credentials

    serve = _serve(monkeypatch, auth_git_server, tmp_path)
    args = {"username": USER, "private_token": TOKEN}
    with serve.app.test_request_context(headers=_git_credentials_header()):
        serve.app.preprocess_request()
        repo = serve._clone_repo("repo", "main", None, args)
        assert TOKEN not in _stored_config(repo)
        assert quote(TOKEN, safe="") not in _stored_config(repo)
        auth_git_server.commit("repo", {"later": "x"})
        serve.pull(repo, "main")
        assert os.path.exists(os.path.join(repo.working_dir, "later"))
    assert request_credentials.get() is None


def test_the_local_guis_clones_keep_their_credentials(
    auth_git_server, tmp_path, monkeypatch
):
    """The local gui's clones are the user's own, and keep their credentials
    in their url so the user's later pulls work."""
    from unfurl.repo import request_credentials

    serve = _serve(monkeypatch, auth_git_server, tmp_path, gui=True)
    args = {"username": USER, "private_token": TOKEN}
    with serve.app.test_request_context(headers=_git_credentials_header()):
        serve.app.preprocess_request()
        assert request_credentials.get() is None
        repo = serve._clone_repo("repo", "main", None, args)
        assert quote(TOKEN, safe="") in _stored_config(repo)


def test_a_clone_made_with_stored_credentials_is_cleaned(
    auth_git_server, tmp_path, monkeypatch
):
    serve = _serve(monkeypatch, auth_git_server, tmp_path)
    args = {"username": USER, "private_token": TOKEN}
    with serve.app.test_request_context():
        path = serve._get_project_repo_dir("repo", "main", args)
    os.makedirs(os.path.dirname(path))
    stored = auth_git_server.url("repo", credentials=True)
    _git("clone", "-q", stored, path, cwd=tmp_path)
    _git("remote", "set-url", "--push", "origin", stored, cwd=path)
    with serve.app.test_request_context(headers=_git_credentials_header()):
        serve.app.preprocess_request()
        repo = serve._get_project_repo("repo", "main", args)
        assert repo
        config = _stored_config(repo)
    assert quote(TOKEN, safe="") not in config, config
    assert auth_git_server.url("repo") in config


def test_credentials_set_during_a_request_end_with_it(tmp_path, monkeypatch):
    """As a batch sets each of its requests' credentials: the request's
    teardown undoes them, whether or not it brought its own."""
    from unfurl.repo import request_credentials
    from unfurl.server import serve

    monkeypatch.setitem(serve.app.config, "UNFURL_GUI_MODE", None)
    monkeypatch.delenv("UNFURL_SERVE_PATH", raising=False)
    with serve.app.test_request_context():
        serve.app.preprocess_request()
        serve.set_request_credentials("someone", "their-token")
        assert request_credentials.get()
    assert request_credentials.get() is None


def test_the_server_pushes_with_credentials_it_doesnt_store(
    auth_git_server, tmp_path, monkeypatch
):
    from unfurl.server import endpoints

    serve = _serve(monkeypatch, auth_git_server, tmp_path)
    monkeypatch.setattr(endpoints, "set_branch_head", lambda *args: None)
    args = {"username": USER, "private_token": TOKEN}
    with serve.app.test_request_context(headers=_git_credentials_header()):
        serve.app.preprocess_request()
        repo = serve._clone_repo("repo", "main", None, args)
        start = repo.revision
        with open(os.path.join(repo.working_dir, "pushed"), "w") as f:
            f.write("x")
        repo.commit_files([os.path.join(repo.working_dir, "pushed")], "push it")
        err = endpoints._push_changes(repo, USER, TOKEN, start, "repo", "main")
        assert err is None, err
    assert repo.revision == _git_out("rev-parse", "main", cwd=auth_git_server.root / "served" / "repo.git")
    assert quote(TOKEN, safe="") not in _stored_config(repo)


def _git_out(*args, cwd):
    import subprocess

    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _hostile_git_config(tmp_path, monkeypatch, url):
    """The host's git config and GIT_* variables, each rewriting ``url`` to
    somewhere that isn't there."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        f'[url "file:///nonexistent/"]\n\tinsteadOf = {url}\n'
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.setenv(
        "GIT_CONFIG_PARAMETERS", f"'url.file:///nonexistent/.insteadOf'='{url}'"
    )


def test_isolated_git_ignores_the_hosts_configuration(tmp_path, monkeypatch):
    from unittest.mock import patch
    from unfurl.repo import Repo as UnfurlRepo, isolate_git

    source = tmp_path / "source"
    source.mkdir()
    _git("init", "-q", "-b", "main", cwd=source)
    _git("commit", "-q", "--allow-empty", "-m", "x", cwd=source)
    url = f"file://{source}"
    with patch.dict(os.environ):
        _hostile_git_config(tmp_path, monkeypatch, url)
        isolate_git(["file"])
        repo = UnfurlRepo.create_working_dir(url, str(tmp_path / "clone"))
        assert repo.revision


@pytest.mark.parametrize("url", ["file:///srv/repo.git", "git://127.0.0.1:9/repo.git"])
def test_isolated_git_uses_only_its_protocols(tmp_path, url):
    import git as gitpython
    from unittest.mock import patch
    from unfurl.repo import isolate_git

    with patch.dict(os.environ):
        isolate_git(["https", "ssh"])
        with pytest.raises(gitpython.exc.GitCommandError) as refused:
            gitpython.cmd.Git().ls_remote(url)
    assert "not allowed" in refused.value.stderr


def test_the_servers_git_allows_https_ssh_and_the_cloud_servers_protocol(monkeypatch):
    from unittest.mock import patch
    from unfurl.server import serve

    monkeypatch.setitem(serve.app.config, "UNFURL_CLOUD_SERVER", "/srv/cloud")
    with patch.dict(os.environ):
        serve._isolate_git()
        assert os.environ["GIT_ALLOW_PROTOCOL"] == "https:ssh:file"
        assert os.environ["GIT_CONFIG_GLOBAL"] == os.devnull


def test_a_submodule_on_the_cloud_server_gets_the_requests_credentials(
    auth_git_server, tmp_path
):
    """With git isolated, a private submodule on the same host is fetched
    with the request's credentials, which no clone stores; one on another
    protocol isn't fetched at all."""
    from unittest.mock import patch
    from unfurl.repo import Repo as UnfurlRepo, isolate_git

    auth_git_server.commit("sub", {"README": "submodule\n"})
    work = auth_git_server.root / "work" / "super"
    work.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=work)
    sub_url = auth_git_server.url("sub", credentials=True)
    _git(
        "-c", "protocol.file.allow=always",
        "submodule", "add", "-q", sub_url, "sub", cwd=work,
    )
    _git("config", "-f", ".gitmodules", "submodule.sub.url", auth_git_server.url("sub"), cwd=work)
    _git("add", "-A", cwd=work)
    auth_git_server.commit("super", {"README": "super\n"})

    with patch.dict(os.environ):
        isolate_git(["http"])
        dest = tmp_path / "clone"
        repo = UnfurlRepo.create_working_dir(
            auth_git_server.url("super"), str(dest), username=USER, password=TOKEN
        )
    assert (dest / "sub" / "README").read_text() == "submodule\n"
    for config in [dest / ".git" / "config", *dest.joinpath(".git", "modules").rglob("config")]:
        assert quote(TOKEN, safe="") not in config.read_text(), config


def test_a_submodule_on_another_protocol_is_refused(
    auth_git_server, tmp_path, monkeypatch
):
    """A file:// submodule would read a repository on the server's own disk,
    another user's clone say: with git isolated, the clone is refused, even
    on a host whose git config allows them."""
    from unittest.mock import patch
    from unfurl.repo import Repo as UnfurlRepo, isolate_git
    from unfurl.util import UnfurlError

    secret = tmp_path / "someone-elses"
    secret.mkdir()
    _git("init", "-q", "-b", "main", cwd=secret)
    (secret / "private").write_text("not yours\n")
    _git("add", "-A", cwd=secret)
    _git("commit", "-q", "-m", "x", cwd=secret)
    work = auth_git_server.root / "work" / "super"
    work.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=work)
    _git(
        "-c", "protocol.file.allow=always",
        "submodule", "add", "-q", f"file://{secret}", "sub", cwd=work,
    )
    _git("add", "-A", cwd=work)
    auth_git_server.commit("super", {"README": "super\n"})

    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text('[protocol "file"]\n\tallow = always\n')
    dest = tmp_path / "clone"
    with patch.dict(os.environ):
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
        isolate_git(["http"])
        with pytest.raises(UnfurlError):
            UnfurlRepo.create_working_dir(
                auth_git_server.url("super"), str(dest), username=USER, password=TOKEN
            )
    assert not (dest / "sub" / "private").exists()


def test_isolated_git_keeps_who_commits(monkeypatch):
    """The identity the server commits as comes from GIT_* variables
    (UNFURL_SET_GIT_USER), not the host's git config: isolating keeps it."""
    from unittest.mock import patch
    from unfurl.repo import isolate_git

    with patch.dict(os.environ):
        monkeypatch.setenv("GIT_AUTHOR_NAME", "server")
        monkeypatch.setenv("GIT_COMMITTER_NAME", "server")
        isolate_git(["https"])
        assert os.environ["GIT_AUTHOR_NAME"] == "server"
        assert os.environ["GIT_COMMITTER_NAME"] == "server"


def test_the_servers_git_has_someone_to_commit_as(monkeypatch, tmp_path):
    """With the host's git config ignored, a server with no
    UNFURL_SET_GIT_USER still commits, as itself."""
    from unittest.mock import patch
    from unfurl.server import serve

    repo = tmp_path / "repo"
    repo.mkdir()
    with patch.dict(os.environ):
        for name in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME", "EMAIL"):
            monkeypatch.delenv(name, raising=False)
        serve._isolate_git()
        _git_plain("init", "-q", cwd=repo)
        _git_plain("commit", "-q", "--allow-empty", "-m", "x", cwd=repo)
        author = _git_out("log", "-1", "--format=%an <%ae>", cwd=repo)
    assert author.startswith("unfurl unfurl-server-"), author


def _git_plain(*args, cwd):
    """git ``args`` with no identity of the test's own."""
    import subprocess

    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)



@pytest.mark.parametrize(
    "gui, local_path, hosted",
    [(False, None, True), (False, ".", False), (True, None, False)],
    ids=["hosted", "developer", "gui"],
)
def test_only_the_hosted_server_is_hosted(monkeypatch, tmp_path, gui, local_path, hosted):
    """The hosted server's clones keep no credentials and its git ignores the
    host's; a developer's `unfurl serve <path>` and the local gui keep the
    user's own."""
    from unfurl.server import serve

    gui_env = _project_env(tmp_path, "gui") if gui else None
    monkeypatch.setitem(serve.app.config, "UNFURL_GUI_MODE", gui_env)
    if local_path:
        monkeypatch.setenv("UNFURL_SERVE_PATH", local_path)
    else:
        monkeypatch.delenv("UNFURL_SERVE_PATH", raising=False)
    assert serve._hosted() == hosted


def test_the_server_under_gunicorn_isolates_git(tmp_path):
    """Imported as gunicorn imports it, the server starts, with its git
    isolated and someone to commit as."""
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if k != "UNFURL_SERVE_PATH"}
    env.update(SERVER_SOFTWARE="gunicorn/test", UNFURL_HOME="")
    env.pop("GIT_AUTHOR_NAME", None)
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os; from unfurl.server import serve; "
            "print(os.environ.get('GIT_ALLOW_PROTOCOL'), os.environ.get('GIT_CONFIG_GLOBAL'),"
            " bool(os.environ.get('GIT_AUTHOR_NAME')))",
        ],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.split()[-3:] == ["https:ssh", os.devnull, "True"], out.stderr[-2000:]
