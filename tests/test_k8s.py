import os
import os.path
import re
import sys
import unittest
import warnings
import json
import subprocess
from shutil import which
import pytest
import time
import yaml
from pathlib import Path

from unfurl.job import JobOptions, Runner
from unfurl.runtime import Status
from unfurl.yamlmanifest import YamlManifest
from unfurl.configurators.k8s import get_kubectl_args
from unfurl.configurators import kompose
from unfurl.configurators.kompose import render_compose
from unfurl.result import serialize_value

from .utils import lifecycle, Step, init_project, run_job_cmd
from click.testing import CliRunner

if not sys.warnoptions:
    # Ansible generates tons of ResourceWarnings
    warnings.simplefilter("ignore", ResourceWarning)

TEST_NS = "octest"
# XXX add test with invalid namespace name
KOMPOSE_NS = "octest-kompose"
# fyi: on failure you may need to:
# kubectl delete namespace octest
# kubectl delete namespace octest-kompose

if "k8s" in os.getenv("UNFURL_TEST_SKIP", ""):
    pytest.skip("UNFURL_TEST_SKIP set", allow_module_level=True)

class TestK8s(unittest.TestCase):
    def test_k8s_config(self):
        os.environ["TEST_SECRET"] = "a secret"
        manifest = YamlManifest(MANIFEST1)
        job = Runner(manifest).run(JobOptions(add=True, startTime=1))
        assert not job.unexpectedAbort
        assert job.status == Status.ok, job.summary()
        # print(job.summary())
        # print(job.out.getvalue())

        # verify secret contents isn't saved in config
        assert "a secret" not in job.out.getvalue()
        assert "YSBzZWNyZXQ" not in job.out.getvalue()  # base64 of "a secret"
        # print (job.out.getvalue())
        assert "<<REDACTED>>" in job.out.getvalue()
        assert not job.unexpectedAbort
        assert job.status == Status.ok, job.summary()
        results = job.json_summary()
        assert results["job"] == {
            "id": "A01110000000",
            "status": "ok",
            "total": 3,
            "ok": 3,
            "error": 0,
            "unknown": 0,
            "skipped": 0,
            "changed": 3,
        }

        manifest = YamlManifest(job.out.getvalue())
        job2 = Runner(manifest).run(JobOptions(workflow="undeploy", startTime=2))
        results = job2.json_summary()
        assert not job2.unexpectedAbort
        assert job2.status == Status.ok, job2.summary()
        assert results == {
            "job": {
                "id": "A01120000000",
                "status": "ok",
                "total": 2,
                "ok": 2,
                "error": 0,
                "unknown": 0,
                "skipped": 0,
                "changed": 2,
            },
            "outputs": {},
            "tasks": [
                {
                    "status": "ok",
                    "target": "testSecret",
                    "operation": "delete",
                    "template": "testSecret",
                    "type": "unfurl.nodes.K8sSecretResource",
                    "targetStatus": "absent",
                    "targetState": "deleted",
                    "changed": True,
                    "configurator": "unfurl.configurators.k8s.ResourceConfigurator",
                    "priority": "required",
                    "reason": "undeploy",
                },
                {
                    "status": "ok",
                    "target": "k8sNamespace",
                    "operation": "delete",
                    "template": "k8sNamespace",
                    "type": "unfurl.nodes.K8sNamespace",
                    "targetStatus": "absent",
                    "targetState": "deleted",
                    "changed": True,
                    "configurator": "unfurl.configurators.k8s.ResourceConfigurator",
                    "priority": "required",
                    "reason": "undeploy",
                },
            ],
        }
        assert len(results["tasks"]) == 2, results

def test_namespace_on_connection():
    manifest = YamlManifest(NO_CLUSTER)
    job = Runner(manifest).run(JobOptions(workflow="check"))
    assert not job.unexpectedAbort
    assert job.status == Status.ok, job.summary()
    config_map_instance = manifest.rootResource.find_instance("a_config_map")
    assert config_map_instance and config_map_instance.attributes["namespace"] == TEST_NS
    assert config_map_instance.query(dict(eval={"is_function_defined": "kubernetes_current_namespace"}))
    assert TEST_NS == config_map_instance.query(dict(eval=dict(kubernetes_current_namespace=None)))

def _get_resources(task):
    # verify resource start (and test get_kubectl_args)
    args = get_kubectl_args(task.inputs.context)
    assert "-n" in args, args
    assert "--insecure-skip-tls-verify" not in args
    cmd = ["kubectl"] + args + "get all -o json".split()
    output = subprocess.run(cmd, capture_output=True).stdout
    return json.loads(output)

def _get_pod_logs(task, name):
    # verify resource start (and test get_kubectl_args)
    args = get_kubectl_args(task.inputs.context)
    cmd = ["kubectl"] + args + ["logs", name, "--tail", "15"]
    proc = subprocess.run(cmd, capture_output=True)
    return proc.stdout, proc.stderr

def _pod_log_test(logs):
    return logs and b"FOO=bar" in logs


STEPS = (
    Step("deploy", Status.ok, changed=-1),  # check that some changes were made
    Step("check", Status.ok, changed=None),
    Step("deploy", Status.ok, changed=0),
    Step("undeploy", Status.absent, changed=-1),
)

@pytest.mark.skipif(
    "k8s" in os.getenv("UNFURL_TEST_SKIP", ""), reason="UNFURL_TEST_SKIP for k8s set"
)
# skip if we don't have kompose installed but require CI to have it
@pytest.mark.skipif(
    not os.getenv("CI") and not which("kompose"), reason="kompose command not found"
)
def test_kompose():
    instance_name = "DockerService1"
    manifest = YamlManifest(KOMPOSE_MANIFEST)
    for i, job in enumerate(lifecycle(manifest, STEPS)):
        assert job.status == Status.ok, job.workflow
        task = None
        for task in job.get_operational_dependencies():
            if task.target.name == instance_name:
                break
        else:
            continue

        task.target.root.attributeManager = task._attributeManager
        resources = _get_resources(task)["items"]
        if STEPS[i].workflow == 'undeploy':
            assert not resources, resources
        elif i == 0:
            assert STEPS[i].workflow == 'deploy'
            assert len(resources) == 4, json.dumps(resources, indent=2)
            for resource in resources:
                if resource["kind"] == "Pod":
                    assert resource["spec"]["containers"][0]["livenessProbe"]["tcpSocket"]["port"] == 8001
                    pod_name = resource["metadata"]["name"]
                    logs, stderr = _get_pod_logs(task, pod_name)
                    count = 0
                    while not _pod_log_test(logs):
                        time.sleep(5)
                        logs, stderr = _get_pod_logs(task, pod_name)
                        if count > 5:
                            assert False, f"timeout trying waiting read logs for {pod_name}, got: {logs} {stderr}"
                        count += 1


START = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
spec:
  service_template:
    imports:
      - repository: unfurl
        file: tosca_plugins/k8s.yaml
    
    topology_template:
      relationship_templates:
        k8sConnection:
          # if a template defines node or capability it will be used
          # as the default relationship when connecting to that node
          default_for: ANY
          # target: k8sCluster
          type: unfurl.relationships.ConnectsTo.K8sCluster
          properties:
            context: {get_env: [UNFURL_TEST_KUBECONTEXT]}
            KUBECONFIG: {get_env: UNFURL_TEST_KUBECONFIG}
"""

NO_CLUSTER = START + f"""
            namespace: {TEST_NS}

      node_templates:
        a_config_map:
          type: unfurl.nodes.K8sResource
          properties:
            definition:
              apiVersion: v1
              kind: ConfigMap
              data:
                key: "3"
"""


BASE = START + """
      node_templates:
        k8sCluster:
          type: unfurl.nodes.K8sCluster
          directives:
            - discover

        k8sNamespace:
         type: unfurl.nodes.K8sNamespace
         requirements:
           - host: k8sCluster
         properties:
           name: %s
"""

SECRET = """\
        testSecret:
          # add metadata, type: Opaque
          # base64 values and omit data from status
          type: unfurl.nodes.K8sSecretResource
          requirements:
            - host: k8sNamespace
          properties:
              name: test-secret
              data:
                uri: "{{ lookup('env', 'TEST_SECRET') }}"

"""

MANIFEST1 = BASE % TEST_NS + SECRET

KOMPOSE = """\
        DockerService1:
          type: tosca:Root
          properties:
            container:
              image: busybox
              command:
                - bin/sh
                - -c
                - while true; do env; sleep 5; done
              ports:
                - 8001:8001
              environment:
                FOO: bar
              labels:
                kompose.volume.size: 10Mi
                kompose.volume.type: emptyDir
              volumes:
                - a_volume:/a_path
            ingress_annotations:
                kubernetes.io/ingress.class: nginx
                kubernetes.io/ingress.provider: nginx
                cert-manager.io/issuer: issuer
          requirements:
            - host: k8sNamespace
          interfaces:
            Standard:
              operations:
                configure:
                  implementation: Kompose
                  inputs:
                    container:
                      eval: .::container
                    annotations:
                      foo.com.mymetadata: "true"
                    labels:
                      # port and healthcheck will be added if missing
                      kompose.service.healthcheck.liveness.tcp_port: null
"""

KOMPOSE_INGRESS = """\
                    expose: foo.com # creates an ingress record
                    ingress_extras:
                      eval:
                        template: |
                          metadata:
                            annotations: {{ ".::ingress_annotations" | eval | map_value | to_json }}                         
                          spec:
                            tls:
                            - hosts:
                                - foo.com
                              secretName: {{ secret is defined or 'tls_secret' }}
"""

# XXX
KOMPOSE2 = """\
        DockerPrivateRegistryService:
          derived_from: unfurl.nodes.DockerHost
          description: run on Kubernetes using Kompose
          properties:
            container:
          interfaces:
            Standard:
              operations:
                configure:
                  implementation: Kompose
                  inputs:
                    image: "{{ SELF.container_image }}"
                    registry_url: "{{ REGISTRY.registry_url }}"
                    registry_user: "{{ REGISTRY.registry_user }}"
                    registry_password: "{{ REGISTRY.registry_password }}"

        DockerComposeService:
          derived_from: unfurl.nodes.DockerHost
          description: run on Kubernetes using Kompose
          properties:
            container:
          interfaces:
            Standard:
              operations:
                configure:
                  implementation: Kompose
                  inputs:
                    files:
                      docker-compose.yml: "{{ 'fixtures/noversion.yaml' | abspath }}"
                    env: "{{ '.configured_by::env' | eval }}"
"""

KOMPOSE_MANIFEST = BASE % KOMPOSE_NS + KOMPOSE

@pytest.mark.skipif(
    "k8s" in os.getenv("UNFURL_TEST_SKIP", ""), reason="UNFURL_TEST_SKIP for k8s set"
)
# skip if we don't have kompose installed but require CI to have it
@pytest.mark.skipif(
    not os.getenv("CI") and not which("kompose"), reason="kompose command not found"
)
def test_kompose_ingress():
    runner = CliRunner()
    with runner.isolated_filesystem():
        init_project(
            runner,
            args=["init", "--mono"],
        )
        with open("ensemble/ensemble.yaml", "w") as f:
            f.write(KOMPOSE_MANIFEST + KOMPOSE_INGRESS)
        # suppress logging
        try:
            old_env_level = os.environ.get("UNFURL_LOGGING")
            result, job, summary = run_job_cmd(
                runner,
                ["--quiet", "deploy", "--dryrun"],
                env={"UNFURL_LOGGING": "critical"},
            )
        finally:
            if old_env_level:
                os.environ["UNFURL_LOGGING"] = old_env_level
        # print(job.manifest.status_summary())
        # output = result.output.strip()
        # print( output )
        # dry run job so rendered files stays in planned:
        ingress_playbook = "ensemble/planned/tasks/busybox-ingress/configure/playbook.yml"
        assert os.path.exists(ingress_playbook)
        with open(ingress_playbook) as f:
            ingress = f.read().strip()
            # print("ingress", ingress)
            assert "foo.com.mymetadata:" in ingress
            assert re.search(r"""annotations:\s+"kubernetes.io/ingress.class": "nginx"\n""", ingress) is not None, ingress
            assert re.search(r"""spec:\s+tls:""", ingress) is not None, ingress


KOMPOSE_INIT = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
spec:
  service_template:
    topology_template:
      node_templates:
        app:
          type: tosca:Root
          interfaces:
            Standard:
              operations:
                configure:
                  implementation: Kompose
                  inputs:
                    service_name: app
                    container:
                      image: wordpress:6
                      volumes:
                        - /var/app/data/html:/var/www/html
                        - /var/app/data/uploads:/uploads:ro
                    init_containers:
                      - image: busybox
                        entrypoint: ["sh", "-c"]
                        command: ["echo hello > /html/ready"]
                        user: "33:33"
                        environment:
                          GREETING: hello
                          UNSET:
                        volumes:
                          - /var/app/data/html:/html
                          - /var/app/data/uploads:/uploads:ro
                      - container_name: second
                        image: alpine
                        command: sleep 1  # split like compose does
"""


def _render_kompose(manifest_src: str):
    runner = Runner(YamlManifest(manifest_src))
    job = runner.static_plan()
    ready, not_ready, errors = job.render()
    return [request.task for request in ready], errors


@pytest.mark.skipif(
    not os.getenv("CI") and not which("kompose"), reason="kompose command not found"
)
def test_kompose_init_containers():
    """Init containers become the workload's initContainers, sharing the app's volumes."""
    tasks, errors = _render_kompose(KOMPOSE_INIT)
    assert tasks and not errors
    definitions = tasks[0].target.attributes["definitions"]
    pod_spec = definitions["app-deployment"]["spec"]["template"]["spec"]
    init_containers = json.loads(json.dumps(serialize_value(pod_spec["initContainers"])))
    fixture_path = Path(__file__).parent / "fixtures" / "kompose_init_containers.yaml"
    try:
        assert init_containers == yaml.safe_load(fixture_path.read_text())
    except (AssertionError, FileNotFoundError):
        fixture_path.write_text(yaml.safe_dump(init_containers, sort_keys=False))
        raise
    # the mounts refer to the volumes kompose generated for the app
    assert {m["name"] for m in init_containers[0]["volumeMounts"]} == {
        v["name"] for v in pod_spec["volumes"]
    }


@pytest.mark.skipif(
    not os.getenv("CI") and not which("kompose"), reason="kompose command not found"
)
@pytest.mark.parametrize(
    "change, message",
    [
        (("- /var/app/data/html:/html", "- /var/app/other:/other"), "must also be mounted by the container"),
        (("image: alpine", "image: alpine\n                        ports: ['80:80']"), "can not publish ports"),
    ],
)
def test_kompose_init_containers_errors(change, message, caplog):
    tasks, errors = _render_kompose(KOMPOSE_INIT.replace(*change))
    assert not tasks and errors
    assert message in caplog.text


@pytest.mark.skipif(
    not os.getenv("CI") and not which("kompose"), reason="kompose command not found"
)
def test_kompose_without_init_containers():
    tasks, errors = _render_kompose(KOMPOSE_INIT.split("                    init_containers:")[0])
    assert tasks and not errors
    definitions = tasks[0].target.attributes["definitions"]
    assert "initContainers" not in definitions["app-deployment"]["spec"]["template"]["spec"]


LITERAL_DOLLARS = ["pa$word", "${X}", "$$", "$(X)", "$", "a $ b"]

KOMPOSE_DOLLARS = f"""
apiVersion: unfurl/v1alpha1
kind: Ensemble
spec:
  service_template:
    topology_template:
      node_templates:
        app:
          type: tosca:Root
          interfaces:
            Standard:
              operations:
                configure:
                  implementation: Kompose
                  inputs:
                    service_name: app
                    container:
                      image: busybox
                      entrypoint: {json.dumps(["sh", *LITERAL_DOLLARS])}
                      command: {json.dumps(["echo", *LITERAL_DOLLARS])}
                      environment: {json.dumps({f"C{i}": v for i, v in enumerate(LITERAL_DOLLARS)})}
                      volumes: ["/var/app/pa$th:/data"]
                    env: {json.dumps({f"E{i}": v for i, v in enumerate(LITERAL_DOLLARS)})}
                    labels: {json.dumps({f"L{i}": v for i, v in enumerate(LITERAL_DOLLARS)})}
                    init_containers:
                      - image: busybox
                        environment: {json.dumps({f"I{i}": v for i, v in enumerate(LITERAL_DOLLARS)})}
                        volumes: ["/var/app/pa$th:/data"]
"""


@pytest.mark.skipif(
    not os.getenv("CI") and not which("kompose"), reason="kompose command not found"
)
def test_kompose_dollars_are_literal(monkeypatch):
    """A ``$`` in a container value reaches Kubernetes as a literal ``$``."""
    monkeypatch.setenv("X", "interpolated")
    tasks, errors = _render_kompose(KOMPOSE_DOLLARS)
    assert tasks and not errors
    definitions = tasks[0].target.attributes["definitions"]
    pod_spec = definitions["app-deployment"]["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    assert container["command"] == ["sh", *LITERAL_DOLLARS]
    assert container["args"] == ["echo", *LITERAL_DOLLARS]
    env = {e["name"]: e["value"] for e in container["env"]}
    for i, value in enumerate(LITERAL_DOLLARS):
        assert env[f"C{i}"] == value
        assert env[f"E{i}"] == value
    annotations = definitions["app-deployment"]["metadata"]["annotations"]
    for i, value in enumerate(LITERAL_DOLLARS):
        assert annotations[f"L{i}"] == value
    init_env = {e["name"]: e["value"] for e in pod_spec["initContainers"][0]["env"]}
    assert init_env == {f"I{i}": v for i, v in enumerate(LITERAL_DOLLARS)}
    assert pod_spec["initContainers"][0]["volumeMounts"] == [
        dict(mountPath="/data", name=container["volumeMounts"][0]["name"])
    ]


@pytest.mark.skipif(
    not os.getenv("CI") and not which("kompose"), reason="kompose command not found"
)
def test_kompose_pod_args_are_literal():
    as_pod = KOMPOSE_DOLLARS.replace(
        "image: busybox\n", 'image: busybox\n                      restart: "no"\n', 1
    )
    tasks, errors = _render_kompose(as_pod)
    assert tasks and not errors
    pod = tasks[0].target.attributes["definitions"]["app-pod"]
    assert pod["spec"]["containers"][0]["args"] == ["echo", *LITERAL_DOLLARS]


def test_render_compose_declares_named_volumes():
    volumes = [
        "data:/data",
        "data:/again:ro",
        "/host:/host",
        "./relative:/relative",
        "~/home:/home",
        "/anonymous",
        dict(type="volume", source="long", target="/long"),
        dict(type="bind", source="/bind", target="/bind"),
    ]
    compose = render_compose(dict(image="busybox", volumes=volumes))
    assert compose["volumes"] == {"data": {}, "long": {}}
    assert "volumes" not in render_compose(dict(image="busybox", volumes=["/host:/host"]))


@pytest.mark.skipif(
    not os.getenv("CI") and not which("kompose"), reason="kompose command not found"
)
def test_kompose_named_volume():
    named = KOMPOSE_INIT.replace("/var/app/data/html:", "html:")
    tasks, errors = _render_kompose(named)
    assert tasks and not errors
    definitions = tasks[0].target.attributes["definitions"]
    assert definitions["html-persistentvolumeclaim"]["kind"] == "PersistentVolumeClaim"
    pod_spec = definitions["app-deployment"]["spec"]["template"]["spec"]
    claims = {v["name"]: v["persistentVolumeClaim"]["claimName"] for v in pod_spec["volumes"]}
    html_mount = pod_spec["initContainers"][0]["volumeMounts"][0]
    assert html_mount["mountPath"] == "/html"
    assert claims[html_mount["name"]] == "html"


KOMPOSE_STUB = """#!/bin/sh
echo "$@" >> "$KOMPOSE_STUB_LOG"
if [ "$1" = version ]; then echo "$KOMPOSE_STUB_VERSION"; fi
"""


@pytest.fixture
def kompose_stub(tmp_path, monkeypatch):
    """Put a fake ``kompose`` first on PATH and return the file logging its invocations."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "kompose"
    stub.write_text(KOMPOSE_STUB)
    stub.chmod(0o755)
    log = tmp_path / "kompose.log"
    log.touch()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("KOMPOSE_STUB_LOG", str(log))
    monkeypatch.setattr(kompose, "_kompose_versions", {})
    return log


def _stub_calls(log: Path) -> list:
    return [line.split()[0] for line in log.read_text().splitlines()]


@pytest.mark.parametrize("version", ["1.26.1 (a9d05d509)", "v1.27.0"])
def test_kompose_too_old(kompose_stub, monkeypatch, caplog, version):
    monkeypatch.setenv("KOMPOSE_STUB_VERSION", version)
    tasks, errors = _render_kompose(KOMPOSE_INIT)
    assert not tasks and errors
    found = version.split()[0].lstrip("v")
    assert f"kompose {found} is too old: Unfurl requires kompose 1.28.0 or later (found " in caplog.text
    assert _stub_calls(kompose_stub) == ["version"]


@pytest.mark.parametrize("version", ["1.28.0", "1.38.0 (a8f5d1cbd)"])
def test_kompose_new_enough(kompose_stub, monkeypatch, version):
    monkeypatch.setenv("KOMPOSE_STUB_VERSION", version)
    tasks, errors = _render_kompose(KOMPOSE_INIT)
    assert tasks and not errors
    assert _stub_calls(kompose_stub) == ["version", "convert"]


def test_kompose_unknown_version(kompose_stub, monkeypatch, caplog):
    monkeypatch.setenv("KOMPOSE_STUB_VERSION", "dev build")
    tasks, errors = _render_kompose(KOMPOSE_INIT)
    assert tasks and not errors
    assert "Could not determine the version of kompose" in caplog.text
    assert _stub_calls(kompose_stub) == ["version", "convert"]


def test_kompose_version_checked_once(kompose_stub, monkeypatch, tmp_path):
    monkeypatch.setenv("KOMPOSE_STUB_VERSION", "1.38.0")
    Runner(YamlManifest(KOMPOSE_INIT)).static_plan()
    assert _stub_calls(kompose_stub) == []
    for _ in range(2):
        tasks, errors = _render_kompose(KOMPOSE_INIT)
        assert tasks and not errors
    assert _stub_calls(kompose_stub) == ["version", "convert", "convert"]
    # a different kompose on PATH is checked again
    other = tmp_path / "other"
    other.mkdir()
    (other / "kompose").write_text(KOMPOSE_STUB)
    (other / "kompose").chmod(0o755)
    monkeypatch.setenv("PATH", f"{other}{os.pathsep}{os.environ['PATH']}")
    tasks, errors = _render_kompose(KOMPOSE_INIT)
    assert tasks and not errors
    assert _stub_calls(kompose_stub) == ["version", "convert", "convert", "version", "convert"]


def test_kompose_container_without_image(caplog):
    unnamed = KOMPOSE_INIT.replace("service_name: app", "").replace("image: wordpress:6", "")
    tasks, errors = _render_kompose(unnamed)
    assert not tasks and errors
    assert "does not specify a container image" in caplog.text


def _with_pull_policy(policy: str) -> str:
    return KOMPOSE_INIT.replace(
        "image: wordpress:6", f"image: wordpress:6\n                      pull_policy: {policy}"
    )


@pytest.mark.skipif(
    not os.getenv("CI") and not which("kompose"), reason="kompose command not found"
)
@pytest.mark.parametrize(
    "policy, expected",
    [("missing", "IfNotPresent"), ("never", "Never"), ("Always", "Always"), ("IfNotPresent", "IfNotPresent")],
)
def test_kompose_pull_policy(policy, expected):
    tasks, errors = _render_kompose(_with_pull_policy(policy))
    assert tasks and not errors
    definitions = tasks[0].target.attributes["definitions"]
    container = definitions["app-deployment"]["spec"]["template"]["spec"]["containers"][0]
    assert container["imagePullPolicy"] == expected


def test_kompose_unsupported_pull_policy(caplog):
    tasks, errors = _render_kompose(_with_pull_policy("build"))
    assert not tasks and errors
    assert 'pull_policy "build" isn\'t supported on Kubernetes' in caplog.text
