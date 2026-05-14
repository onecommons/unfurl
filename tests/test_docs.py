import fnmatch
import pathlib
import shutil
import subprocess
import unittest
import os
import glob
from shutil import which
import pytest

from .test_dsl import _to_yaml
from toscaparser.tosca_template import ToscaTemplate
from unfurl.localenv import LocalConfig
from unfurl.yamlmanifest import YamlManifest
from unfurl.util import _basepath
from unfurl.yamlloader import YamlConfig
from unfurl.spec import ToscaSpec
from tosca import global_state
from unfurl.testing import (
    CliRunner,
    assert_no_mypy_errors,
    run_cmd,
    run_job_cmd,
    fs_runner,
    init_project,
)
from unfurl.eval import Ref, RefContext
from unfurl.runtime import NodeInstance

basedir = os.path.join(os.path.dirname(__file__), "..", "docs", "examples")


class DocsTest(unittest.TestCase):
    def test_schemas(self):
        assert LocalConfig(os.path.join(basedir, "unfurl.yaml"))
        assert YamlManifest(path=os.path.join(basedir, "ensemble.yaml"))
        assert YamlConfig(
            path=os.path.join(basedir, "job.yaml"),
            schema=os.path.join(_basepath, "changelog-schema.json"),
        )
        path = os.path.join(basedir, "service-template.yaml")
        serviceTemplate = YamlConfig(path=path)
        assert ToscaSpec(serviceTemplate.config, path=path)

    def test_python_snippets(self):
        # examples generated like:
        # UNFURL_EXPORT_PYTHON_STYLE=concise unfurl -vv export --format python docs/examples/configurators-6.yaml
        python_files = glob.glob(os.path.join(basedir, "*.py"))

        required_imports = """
import unfurl
import tosca
from tosca import Attribute, Eval, Property, operation, GB, MB, TopologyInputs
"""

        def skip(py_file):
            for skip in [
                "*quickstart_*",
                "*/inputs.py",
                "*node-types-2.py",
                "*tosca-node-template.py",
                "*tosca-outline.py",  # has invalid imports
            ]:
                if fnmatch.fnmatch(py_file, skip):
                    print(f"Skipping {py_file}")
                    return True
            return False

        global_state.mode = "parse"
        for py_file in python_files:
            if skip(py_file):
                continue
            with self.subTest(py_file=py_file):
                with open(py_file, "r") as f:
                    code = f.read()

                # Add the required imports at the beginning of the code
                full_code = required_imports + "\n" + code

                # Execute the code to ensure it's valid
                print(f"Executing {py_file}")
                exec(full_code, {})

    def test_python_example(self):
        basedir = os.path.join(os.path.dirname(__file__), "..", "docs", "examples")
        yaml_template = ToscaTemplate(
            path=os.path.join(basedir, "service-template.yaml")
        )
        with open(os.path.join(basedir, "service_template.py")) as pyfile:
            from_py = _to_yaml(pyfile.read(), True)
        assert (
            from_py["topology_template"]["outputs"]
            == yaml_template.topology_template._tpl_outputs()
        )
        assert (
            from_py["topology_template"]["inputs"]
            == yaml_template.topology_template._tpl_inputs()
        )

    def test_eval_examples(self):
        """Test examples from docs/processing.rst Evaluation Semantics section"""

        # Create test resource with attributes matching documentation examples
        resourceDef = {
            "name": "test",
            "b": [1, 2, 3],
            "n": {"n": {"n": "value"}},
            "f": {"a": 1, "b": 2},
            "d": {"a": "va", "b": "vb"},
            "x": [
                {"a": [{"c": 1}, {"c": 2}, {"b": "exists"}]},
            ],
            "e": {"a1": {"b1": "v1"}, "a2": {"b2": "v2"}},
        }
        resource = NodeInstance("test", resourceDef)

        # Note: these return lists because resolve() returns a list
        test_cases = [
            # Filter examples - existence filters
            ("d[a=va]", [{"a": "va", "b": "vb"}]),
            ("d[a=va]::b", ["vb"]),
            ("x::a[b]::c", [1, 2]),
            ("x::a[b=exists]::c", [1, 2]),
            ("x::a[b!=exists]::c", []),
            ("x::a[!b]::c", []),
            # Wildcard examples
            ("d::*", set(["va", "vb"])),
            ("e::*::b2", ["v2"]),
        ]

        for expr, expected in test_cases:
            with self.subTest(expr=expr):
                ref = Ref({"eval": expr})
                result = ref.resolve(RefContext(resource, trace=0))
                if isinstance(expected, set):
                    self.assertEqual(set(result), expected)
                else:
                    self.assertEqual(result, expected, f"expr was: {expr}")

        # List navigation
        resourceDef4 = {
            "b": [1, 2, 3],
            "x": {"a": [{"c": 1}, {"c": 2}, {"c": 3}, {"c": 4}]},
        }
        resource4 = NodeInstance("test", resourceDef4)

        initial_test_cases = [
            ("b::0", [1]),
            ("b::1", [2]),
            ("x::a::c", [1, 2, 3, 4]),
        ]

        for expr, expected in initial_test_cases:
            with self.subTest(expr=expr):
                ref = Ref({"eval": expr})
                result = ref.resolve(RefContext(resource4, trace=0))
                self.assertEqual(result, expected, f"expr was: {expr}")

        # Test ? operator examples
        resourceDef3 = {
            "b": [1, 2, 3],
            "x": [{"a": [{"c": 1}, {"c": 2}]}, {"a": [{"c": 3}, {"c": 4}]}],
        }
        resource3 = NodeInstance("test", resourceDef3)

        q_test_cases = [
            ("x::a::c", [1, 2, 3, 4]),
            ("x::a?::c", [1, 2]),  # only first match of 'a'
            ("b?::2", [3]),  # first match, then get index 2
            (
                "x?::a[c=4]",
                [[{"c": 3}, {"c": 4}]],
            ),  # first x that has a with c=4
        ]

        for expr, expected in q_test_cases:
            with self.subTest(expr=expr):
                ref = Ref({"eval": expr})
                result = ref.resolve(RefContext(resource3, trace=0))
                self.assertEqual(result, expected, f"expr was: {expr}")

        # # Flattening example
        resourceDef = {"x": [{"a": [{"c": 1}, {"c": 2}]}, {"a": [{"c": 3}, {"c": 4}]}]}
        resource = NodeInstance("test", resourceDef)
        ref = Ref({"eval": "x::a::c"})
        assert ref.resolve(RefContext(resource, trace=0)) == [1, 2, 3, 4]

        # top-level foreach
        ref = Ref(
            dict(
                eval="$numbers", foreach="{{ item * 2 }}", vars=dict(numbers=[1, 2, 3])
            )
        )
        assert ref.resolve(RefContext(resource, trace=0)) == [2, 4, 6]


def test_inputs():
    basedir = os.path.join(os.path.dirname(__file__), "..", "docs", "examples")
    yaml_template = ToscaTemplate(path=os.path.join(basedir, "tosca_inputs.yaml"))

    interfaces = yaml_template.topology_template.node_templates["example"].interfaces
    create_op = interfaces[0]
    assert create_op.name == "create"
    default_op = interfaces[1]
    assert default_op.name == "default"
    assert create_op.inputs == {"foo": "base:create", "bar": "derived:create"}
    assert default_op.inputs == {"foo": "derived:default", "bar": "derived:default"}

    with open(os.path.join(basedir, "tosca_inputs.py")) as pyfile:
        from_py = _to_yaml(pyfile.read(), False)
    py_template = ToscaTemplate(yaml_dict_tpl=from_py)
    interfaces = py_template.topology_template.node_templates["example"].interfaces
    create_op = interfaces[0]
    assert create_op.name == "create"
    default_op = interfaces[1]
    assert default_op.name == "default"
    assert create_op.inputs == {"foo": "base:create", "bar": "derived:create"}
    assert default_op.inputs == {"foo": "derived:default", "bar": "derived:default"}


SAVE_TMP = os.getenv("UNFURL_TEST_TMPDIR")


@pytest.fixture(scope="module", params=["doctest-py", "doctest-yaml"])
def quickstart(request):
    namespace = request.param
    use_yaml = "yaml" in namespace
    runner = CliRunner()
    with runner.isolated_filesystem(SAVE_TMP) as test_dir:
        if SAVE_TMP:
            print("saving to", test_dir)
        init_args = ["init", "myproject", "--empty", "--design", "--var", "std", "true"]
        run_cmd(runner, init_args)
        os.chdir("myproject")
        base = pathlib.Path(basedir)
        if use_yaml:
            ext = "yaml"
        else:
            ext = "py"
        shutil.copy(
            base / f"quickstart_service_template.{ext}", "service_template." + ext
        )
        extra = ""
        if use_yaml:
            ext = "yaml"
        else:
            ext = "py"
        shutil.copy(
            base / f"quickstart_service_template.{ext}", "service_template." + ext
        )
        extra = ""
        if use_yaml:
            extra += " --var serviceTemplate service_template.yaml"
        run_cmd(
            runner,
            "init production --skeleton aws --use-environment production" + extra,
        )
        run_cmd(
            runner,
            "init development --skeleton k8s --use-environment development --var namespace "
            + namespace
            + extra,
        )
        with open(base / f"quickstart_deployment_blueprints.{ext}") as src_file:
            deployment_blueprint = src_file.read()
        with open("service_template." + ext, "a") as f:  # append
            f.write(deployment_blueprint)

        yield namespace, runner


@pytest.fixture(scope="module")
def namespace(quickstart):
    namespace, runner = quickstart
    if "k8s" in os.getenv("UNFURL_TEST_SKIP", ""):
        yield runner
    else:
        os.system(f"kubectl create namespace {namespace}")
        # skip setting context since k8s configurator always sets a namespace now
        # old_namespace = subprocess.run("kubectl config view --minify -o jsonpath='{..namespace}'", shell=True, capture_output=True).stdout.strip()
        # os.system(f"kubectl config set-context --current --namespace {namespace}")
        yield runner
        # os.system(f"kubectl config set-context --current --namespace {old_namespace}")
        os.system(f"kubectl delete namespace {namespace} --wait=false")


def test_quickstart_setup(quickstart):
    # test project setup by invoking the fixture even if the full tests are skipped
    namespace, runner = quickstart
    assert runner


@pytest.mark.skipif(
    "slow" in os.getenv("UNFURL_TEST_SKIP", ""), reason="UNFURL_TEST_SKIP set"
)
def test_quickstart_prod(quickstart):
    namespace, runner = quickstart
    run_cmd(
        runner,
        "-vvv deploy --approve --dryrun --jobexitcode error production".split(),
    )


# skip if we don't have kompose installed but require CI to have it
@pytest.mark.skipif(
    not os.getenv("CI") and not which("kompose"), reason="kompose command not found"
)
@pytest.mark.skipif(
    "slow" in os.getenv("UNFURL_TEST_SKIP", ""), reason="UNFURL_TEST_SKIP set"
)
def test_quickstart_dev(namespace):
    runner = namespace
    dryrun = "--dryrun" if "k8s" in os.getenv("UNFURL_TEST_SKIP", "") else ""
    result, job, summary = run_job_cmd(
        runner,
        f"-vvv deploy --approve {dryrun} development".split(),
        print_result=True,
    )
    summary["job"].pop("id")
    assert summary["job"].pop("ok") >= 8
    assert summary["job"].pop("changed") >= 8
    blocked = summary["job"].pop("blocked", None)
    assert blocked is None or blocked == 2  # dns blocked on ingress
    assert {
        "status": "error",
        "total": 10,
        "error": 0,
        "unknown": 0,
        "skipped": 0,
    } == summary["job"]
    result, job, summary = run_job_cmd(
        runner,
        f"-vvv teardown --approve {dryrun} development".split(),
        print_result=True,
    )
    summary["job"].pop("id")
    assert summary["job"].pop("total") >= 4
    errors = summary["job"].pop("error")
    assert errors <= 1
    assert summary["job"].pop("ok") >= 3
    assert summary["job"].pop("changed") >= 4
    assert {
        "status": "error" if errors else "ok",
        "unknown": 0,
        "skipped": 0,
    } == summary["job"]


@pytest.mark.skipif(
    "slow" in os.getenv("UNFURL_TEST_SKIP", ""), reason="UNFURL_TEST_SKIP set"
)
@pytest.mark.parametrize("path", ["artifact2.py", "tosca-interfaces.py"])
def test_mypy(path):
    # assert mypy ok
    basepath = os.path.join(os.path.dirname(__file__), "..", "docs", "examples", path)
    assert_no_mypy_errors(basepath)  # , "--disable-error-code=override")


unfurl_yaml = """\
apiVersion: unfurl/v1.0.0
kind: Project

environments:
  aws:
    connections:
      primary_provider:
        type: unfurl.relationships.ConnectsTo.AWSAccount
        properties:
          AWS_DEFAULT_REGION: us-east-1
"""


@pytest.mark.parametrize("ext", ["py", "yaml"])
def test_artifact4(fs_runner, ext):
    artifact4_path = os.path.join(
        os.path.dirname(__file__), "..", "docs", "examples", f"artifact4.{ext}"
    )
    if ext == "yaml":
        args = "init --mono --var serviceTemplate service_template.yaml".split()
    else:
        args = None
    init_project(
        fs_runner, args=args, path=artifact4_path, path_dest=f"service_template.{ext}"
    )
    with open("unfurl.yaml", "w") as dst_file:
        dst_file.write(unfurl_yaml)

    result, job, summary = run_job_cmd(
        fs_runner, "deploy --approve --jobexitcode error"
    )
    assert len(job.workDone) == 1
    task = list(job.workDone.values())[0]
    assert task._artifact.name == "default_implementation"

    result, job, summary = run_job_cmd(
        fs_runner, "deploy --use-environment aws -a --force --jobexitcode error"
    )
    assert len(job.workDone) == 1
    task = list(job.workDone.values())[0]
    assert task._artifact.name == "aws_implementation"
