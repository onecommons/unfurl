from pathlib import Path
from .utils import isolated_lifecycle
from unfurl.yamlmanifest import YamlManifest
from unfurl.job import Runner

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
