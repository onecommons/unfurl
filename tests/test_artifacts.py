import os
from pathlib import Path
from click.testing import CliRunner, Result

from .utils import DEFAULT_STEPS, isolated_lifecycle, lifecycle
from unfurl.yamlmanifest import YamlManifest
from unfurl.job import Runner, run_job

ensemble = """
apiVersion: unfurl/v1alpha1
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
        assert (
            localhost.attributes["package_manager"] == "homebrew"
        ), localhost.attributes
    else:  # assume running on a linus
        localhost.attributes["package_manager"] in [
            "apt",
            "yum",
            "rpm",
        ], localhost.attributes["package_manager"]
    assert localhost.attributes["architecture"]
    assert localhost.attributes["architecture"] == localhost.query(
        ".capabilities::architecture"
    )


def test_lifecycle():
    src_path = str(Path(__file__).parent / "examples" / "build_artifact-ensemble.yaml")
    # verify that the build artifact got installed as part of an external job
    for job in isolated_lifecycle(src_path):
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
    env = dict(UNFURL_HOME="")  # no home
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
    assert job.get_outputs()["outputVar"].strip() == "Artifact: deploy_artifact intent deploy contents of deploy_artifact parent: configuration"


collection_ensemble = """
apiVersion: unfurl/v1alpha1
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
                      run:
                        - "{{ 'test' | mdellweg.filters.repr }}"
                      done:
                        status: ok
changes: []
"""

def test_collection_artifact():
    cli_runner = CliRunner()
    with cli_runner.isolated_filesystem(
        os.getenv("UNFURL_TEST_TMPDIR")
    ) as tmp_path:
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
        assert run1.json_summary()["external_jobs"][0]["job"]["ok"] == 2, run1.summary()
        # print ( run1.json_summary(True) )
