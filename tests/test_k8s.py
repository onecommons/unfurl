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

from unfurl.job import JobOptions, Runner
from unfurl.runtime import Status
from unfurl.yamlmanifest import YamlManifest
from unfurl.configurators.k8s import get_kubectl_args

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

@pytest.mark.skipif(
    "k8s" in os.getenv("UNFURL_TEST_SKIP", ""), reason="UNFURL_TEST_SKIP set"
)
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


def _get_resources(task):
    # verify resource start (and test get_kubectl_args)
    args = get_kubectl_args(task)
    assert "-n" in args, args
    assert "--insecure-skip-tls-verify" not in args
    cmd = ["kubectl"] + args + "get all -o json".split()
    output = subprocess.run(cmd, capture_output=True).stdout
    return json.loads(output)

def _get_pod_logs(task, name):
    # verify resource start (and test get_kubectl_args)
    args = get_kubectl_args(task)
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


BASE = """
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
                      - "{{ 'fixtures/noversion.yaml' | abspath }}"
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
        ingress_playbook = "ensemble/active/tasks/busybox-ingress/configure/playbook.yml"
        assert os.path.exists(ingress_playbook)
        with open(ingress_playbook) as f:
            ingress = f.read().strip()
            # print("ingress", ingress)
            assert "foo.com.mymetadata:" in ingress
            assert re.search(r"""annotations:\s+kubernetes.io/ingress.class: nginx""", ingress) is not None, ingress
            assert re.search(r"""spec:\s+tls:""", ingress) is not None, ingress
