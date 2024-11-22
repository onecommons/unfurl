import fnmatch
import pathlib
import shutil
import unittest
import os
import glob
from shutil import which
import pytest

from .test_dsl import _to_yaml
from toscaparser.tosca_template import ToscaTemplate
from unfurl.localenv import LocalConfig
from unfurl.yamlmanifest import YamlManifest, _basepath
from unfurl.yamlloader import YamlConfig
from unfurl.spec import ToscaSpec
from tosca import global_state
from unfurl.testing import CliRunner, run_cmd

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
            for skip in ["*quickstart_*", "*inputs.py", "*node-types-2.py", "*tosca-node-template.py"]:
                if fnmatch.fnmatch(py_file, skip):
                    return True
            return False

        global_state.mode = "spec"
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
        assert from_py["topology_template"]["outputs"] == yaml_template.topology_template._tpl_outputs()
        assert from_py["topology_template"]["inputs"] == yaml_template.topology_template._tpl_inputs()

ensemble_template = """
apiVersion: unfurl/v1alpha1
spec:
  service_template:
    +?include: service_template.py
    repositories:
     std:
       url: https://unfurl.cloud/onecommons/std.git
"""

@pytest.mark.skipif(
    "k8s" in os.getenv("UNFURL_TEST_SKIP", ""), reason="UNFURL_TEST_SKIP for k8s set"
)
# skip if we don't have kompose installed but require CI to have it
@pytest.mark.skipif(
    not os.getenv("CI") and not which("kompose"), reason="kompose command not found"
)
def test_quickstart():
    runner = CliRunner()
    with runner.isolated_filesystem():
        run_cmd(runner, ["init", "myproject", "--empty"])
        os.chdir("myproject")
        with open("ensemble-template.yaml", "w") as f:
            f.write(ensemble_template)
        base = pathlib.Path(basedir)
        shutil.copy(base / "quickstart_service_template.py", "service_template.py")
        run_cmd(runner, "validate")
        run_cmd(runner, "init production --skeleton aws --use-environment production")
        run_cmd(runner, "init development --skeleton k8s --use-environment development")
        with open(base / "quickstart_deployment_blueprints.py") as src_file:
           deployment_blueprint  = src_file.read()
        with open("service_template.py", "a") as f:
            f.write(deployment_blueprint)
        run_cmd(runner, "plan production")
        if "slow" not in os.getenv("UNFURL_TEST_SKIP", ""):
            run_cmd(runner, "deploy --dryrun --approve development")
