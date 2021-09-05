from pathlib import Path
from .utils import isolated_lifecycle


def test_lifecycle():
    src_path = str(Path(__file__).parent / "examples" / "build_artifact-ensemble.yaml")
    # verify that the build artifact got installed as part of an external job
    for job in isolated_lifecycle(src_path):
        if job.step.step == 2:  # deploy
            summary = job.json_summary()
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
            assert "external_jobs" not in job.json_summary()


def test_lifecycle_no_home():
    env = dict(UNFURL_HOME="")  # no home
    src_path = str(Path(__file__).parent / "examples" / "build_artifact-ensemble.yaml")
    for job in isolated_lifecycle(src_path, env=env):
        # verify that the build artifact got installed as part of an job request
        summary = job.json_summary()
        assert "external_jobs" not in summary
        if job.step.step == 2:  # deploy
            # check that the artifacts were deployed too
            assert summary["job"] == {
                "id": "A01120000000",
                "status": "ok",
                "total": 3,
                "ok": 3,
                "error": 0,
                "unknown": 0,
                "skipped": 0,
                "changed": 3,
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
