from io import StringIO
import os
import sys
from pathlib import Path
from click.testing import CliRunner, Result

from .utils import DEFAULT_STEPS, isolated_lifecycle, lifecycle
from unfurl.yamlmanifest import YamlManifest, save_task
from unfurl.job import Runner, run_job, JobOptions
from tosca.python2yaml import PythonToYaml, python_src_to_yaml_obj
from unfurl.yamlloader import ImportResolver, load_yaml, yaml
from unfurl.util import API_VERSION

ensemble = f"""
apiVersion: {API_VERSION}
kind: Ensemble
spec:
  service_template:
    imports:
      - repository: unfurl
        file: tosca_plugins/localhost.yaml

    topology_template:
      node_templates:
        localhost:
          type: unfurl.nodes.Localhost
"""


def test_localhost():
    runner = Runner(YamlManifest(ensemble))
    run1 = runner.run()
    assert not run1.unexpectedAbort, run1.unexpectedAbort.get_stack_trace()
    assert len(run1.workDone) == 1, run1.summary()
    assert run1.json_summary()["job"]["ok"] == 1, run1.summary()
    localhost = runner.manifest.get_root_resource().find_resource("localhost")
    assert localhost
    if localhost.attributes["os_type"] == "Darwin":
        assert localhost.attributes["package_manager"] == "homebrew", (
            localhost.attributes
        )
    else:  # assume running on a linus
        (
            localhost.attributes["package_manager"]
            in [
                "apt",
                "yum",
                "rpm",
            ],
            localhost.attributes["package_manager"],
        )
    assert localhost.attributes["architecture"]
    assert localhost.attributes["architecture"] == localhost.query(
        ".capabilities::architecture"
    )


def test_lifecycle():
    src_path = str(Path(__file__).parent / "examples" / "build_artifact-ensemble.yaml")
    # verify that the build artifact got installed as part of an external job
    env = dict(UNFURL_HOME="./unfurl_home")  # create a home project in the temporary
    for job in isolated_lifecycle(src_path, env=env):
        if job.step.step == 2:  # deploy
            summary = job.json_summary()
            # print("with home", job.json_summary(True))
            assert "unfurl_home" in summary["external_jobs"][0]["ensemble"]
            # check that the artifact were deployed as an external job
            assert summary["job"] == {
                "id": "A01120000000",
                "status": "ok",
                "total": 1,
                "ok": 1,
                "error": 0,
                "unknown": 0,
                "skipped": 0,
                "changed": 1,
            }
            assert summary["external_jobs"][0]["job"] == {
                "id": "A01120000000",
                "status": "ok",
                "total": 2,
                "ok": 2,
                "error": 0,
                "unknown": 0,
                "skipped": 0,
                "changed": 2,
            }
        else:
            assert "external_jobs" not in job.json_summary(), job.json_summary(True)


def test_lifecycle_no_home():
    env = dict(
        UNFURL_HOME=""
    )  # no home (not needed, this is the default for isolated_lifecycle)
    src_path = str(Path(__file__).parent / "examples" / "build_artifact-ensemble.yaml")
    for job in isolated_lifecycle(src_path, env=env):
        # verify that the build artifact got installed as part of an job request
        summary = job.json_summary()
        # assert "external_jobs" not in summary
        if job.step.step == 2:  # deploy
            # check that the artifacts were deployed in the same ensemble as this one
            assert summary["external_jobs"][0]["ensemble"] == job.manifest.path
            assert summary["external_jobs"][0]["job"] == {
                "id": "A01120000000",
                "status": "ok",
                "total": 2,
                "ok": 2,
                "error": 0,
                "unknown": 0,
                "skipped": 0,
                "changed": 2,
            }
        elif job.step.step == 5:  # undeploy
            # check that the artifacts weren't deleted
            assert summary["job"] == {
                "id": "A01150000000",
                "status": "ok",
                "total": 1,
                "ok": 1,
                "error": 0,
                "unknown": 0,
                "skipped": 0,
                "changed": 1,
            }


def test_target_and_intent():
    src_path = str(Path(__file__).parent / "examples" / "deploy_artifact.yaml")
    job = run_job(src_path, dict(skip_save=True))
    assert (
        job.get_outputs()["outputVar"].strip()
        == "Artifact: deploy_artifact intent deploy contents of deploy_artifact parent: configuration"
    )


collection_ensemble = """
apiVersion: %s
kind: Ensemble
spec:
  service_template:
    imports:
      - repository: unfurl
        file: tosca_plugins/artifacts.yaml

    topology_template:
      node_templates:
        test:
          type: unfurl.nodes.Generic
          artifacts:
            mdellweg.filters:
              type: artifact.AnsibleCollection
              file: mdellweg.filters
          interfaces:
            Standard:
              operations:
                configure:
                  implementation:
                    className: Template
                    dependencies:
                      - mdellweg.filters  # a small collection not currently installed
                  inputs:
                      run:  # just calls repr()
                        - "{{ 'test' | mdellweg.filters.repr }}"
                      done:
                        status: ok
changes: []
""" % API_VERSION


def test_collection_artifact():
    cli_runner = CliRunner()
    with cli_runner.isolated_filesystem(os.getenv("UNFURL_TEST_TMPDIR")) as tmp_path:
        os.environ["ANSIBLE_COLLECTIONS_PATH"] = tmp_path
        runner = Runner(YamlManifest(collection_ensemble))
        run1 = runner.run()
        assert not run1.unexpectedAbort, run1.unexpectedAbort.get_stack_trace()
        assert len(run1.workDone) == 1, run1.summary()
        # print ( run1.json_summary(True) )
        assert run1.json_summary()["external_jobs"][0]["job"]["ok"] == 4, run1.summary()

        coll_path = os.path.join(tmp_path, "ansible_collections")
        assert "mdellweg" in os.listdir(coll_path)

        runner = Runner(YamlManifest(collection_ensemble))
        run1 = runner.run()
        assert not run1.unexpectedAbort, run1.unexpectedAbort.get_stack_trace()
        assert len(run1.workDone) == 1, run1.summary()
        summary = run1.json_summary(add_rendered=True)
        # print ( run1.json_summary(True, add_rendered=True) )
        assert summary["external_jobs"][0]["job"]["ok"] == 2, run1.summary()
        if sys.version_info[1] > 8:
            # mdellweg.filter.repr result
            assert summary["tasks"][0]["output"]["run"] == ["'test'"], summary


def _to_yaml(python_src: str):
    namespace: dict = {}
    tosca_tpl = python_src_to_yaml_obj(python_src, namespace)
    # yaml.dump(tosca_tpl, sys.stdout)
    return tosca_tpl


def _get_python_manifest(pyfile, yamlfile=None):
    basepath = os.path.join(os.path.dirname(__file__), "examples/")
    with open(os.path.join(basepath, pyfile)) as f:
        python_src = f.read()
        manifest_tpl = get_manifest_tpl(python_src)
        yaml.dump(manifest_tpl, sys.stdout)
        if yamlfile:
            with open(os.path.join(basepath, yamlfile)) as f:
                yaml_src = f.read()
                yaml_tpl = yaml.load(yaml_src)
                assert yaml_tpl == manifest_tpl

    return manifest_tpl

def get_manifest_tpl(python_src):
    py_tpl = _to_yaml(python_src)
    print( py_tpl )
    manifest_tpl = dict(
            apiVersion=API_VERSION,
            kind="Ensemble",
            spec=dict(service_template=py_tpl),
        )
    return manifest_tpl

service_template = """
import unfurl
import tosca

class NodePool(tosca.DataEntity):
    name: str
    count: int = 1

class Cluster(tosca.nodes.Root):
    node_pool: NodePool

    def set_connection(self):
        outputs: dict = tosca.global_state_context().vars["outputs"]
        # check that default_connection was found as a connection for this node:
        assert tosca.global_state_context().vars["connections"]["default_connection"]
        assert isinstance(self, Cluster), self
        self.default_connection.node_pool_name = outputs["name"]

    default_connection: tosca.relationships.ConnectsTo = tosca.Requirement(factory=lambda: tosca.relationships.ConnectsTo(_default_for="SELF"))

    config: unfurl.artifacts.TemplateOperation = unfurl.artifacts.TemplateOperation(file="", resultTemplate = tosca.Eval(set_connection))

    def configure(self, **kw):
        # this can't be converted to declarative yaml (the node_pool.count assignment will throw an exception)
        # so this method will be invoked during task render time
        node_pool = self.node_pool
        node_pool.count = 3
        node_pool.name = node_pool.name + "-1"
        return self.config.execute(done=dict(outputs=node_pool.to_yaml()))

cluster = Cluster(node_pool=NodePool(name="node_pool"))
"""
def test_render_operation():
    # test non-declarative TOSCA operations that execute an artifact at render time
    manifest_tpl = get_manifest_tpl(service_template)
    manifest = YamlManifest(manifest_tpl)
    job = Runner(manifest)
    job = job.run(JobOptions(out=StringIO()))
    assert job
    assert len(job.workDone) == 1, len(job.workDone)
    task = list(job.workDone.values())[0]
    assert task.outputs["count"] == 3
    assert task.outputs["name"] == "node_pool-1"
    # make sure we serialized custom attribute on the default_connection:
    assert "node_pool_name: node_pool-1" in job.out.getvalue()

def test_artifact_dsl():
    manifest_tpl = _get_python_manifest("dsl_artifacts.py")
    manifest = YamlManifest(manifest_tpl)
    job = Runner(manifest)
    job = job.run(JobOptions(skip_save=True))
    assert job
    assert len(job.workDone) == 1, len(job.workDone)
    task = list(job.workDone.values())[0]
    assert task._arguments() == {
        "do_region": "nyc3",
        "doks_k8s_version": "1.30",
        "extra": "extra",
    }
    assert task.configSpec.outputs
    mycluster = manifest.rootResource.find_instance("mycluster")
    assert mycluster.attributes["do_id"] == "ABC"
    assert mycluster.artifacts["clusterconfig"].file == "my_custom_kubernetes_tf_module"


def test_artifact_syntax():
    manifest_tpl = _get_python_manifest("artifact1.py", "artifact1.yaml")
    manifest = YamlManifest(manifest_tpl)
    job = Runner(manifest)
    job = job.run(JobOptions(skip_save=True))
    assert job
    assert len(job.workDone) == 1, len(job.workDone)
    task = list(job.workDone.values())[0]
    assert (
        save_task(task)["digestKeys"]
        == "arguments,main,::test::.artifacts::configurator-artifacts--terraform::main,::test::.artifacts::configurator-artifacts--terraform::contents"
    )
    assert (
        manifest.rootResource.find_instance("test").attributes["output_attribute"]
        == "test node hello:1"
    )
