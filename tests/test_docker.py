import unittest
import os
import pickle
from unfurl.yamlmanifest import YamlManifest
from unfurl.job import Runner, JobOptions
from unfurl.support import Status, ContainerImage
import toscaparser.repositories
from pathlib import Path
from .utils import isolated_lifecycle, DEFAULT_STEPS, Step

manifest = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
spec:
  service_template:
    imports:
      - repository: unfurl
        file: configurators/templates/docker.yaml
    repositories:
      docker_hub:
        url: https://index.docker.io
        credential:
           user: a_user
           token: a_password
    topology_template:
      node_templates:
        container1:
          type: unfurl.nodes.Container.Application.Docker
          properties:
            name: test_docker
          artifacts:
            image:
              type: tosca.artifacts.Deployment.Image.Container.Docker
              file: busybox
          interfaces:
            Standard:
              inputs:
                configuration:
                  command: ["echo", "hello"]
                  detach:  no
                  output_logs: yes

        test1:
          type: unfurl.nodes.Container.Application.Docker
          artifacts:
            image:
              type: tosca.artifacts.Deployment.Image.Container.Docker
              file: repo/image
              repository: docker_hub
          interfaces:
            Standard:
              operations:
                create:
                  implementation:
                    primary: community.docker
                    className: unfurl.configurators.ansible.AnsibleConfigurator
                  outputs:
                    registry:
                    image_path:
                  inputs:
                    playbook:
                      q:
                        - set_fact:
                            image: "{{ '.artifacts::image' | ref }}"
                            image_path: "{{ {'get_artifact': ['SELF', 'image']} | ref }}"
                        - set_fact:
                            registry: "{{ image.repository }}"
                        - community.docker.docker_login:
                             # https://docs.ansible.com/ansible/latest/modules/docker_login_module.html#docker-login-module
                             # https://github.com/ansible/ansible/blob/stable-2.8/lib/ansible/modules/cloud/docker/docker_login.py
                             username: "{{ registry.credential.user }}"
                             password: "{{ registry.credential.token }}"
                             registry_url: "{{ registry.url }}"
                          when: registry and registry.credential
"""

def test_container_image():
    args = ContainerImage.split("busybox:latest")
    assert args == ('busybox', 'latest', None, None)
    image = ContainerImage.make("busybox:latest")
    assert isinstance(image, ContainerImage)
    assert image == "busybox:latest"
    assert ContainerImage.resolve_name("onecommons/unfurl", "unfurl:45245628") == "onecommons/unfurl:45245628"


@unittest.skipIf("docker" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
class DockerTest(unittest.TestCase):
    def setUp(self):
        try:
            # Ansible generates tons of ResourceWarnings
            warnings.simplefilter("ignore", ResourceWarning)
        except:
            # python 2.x doesn't have ResourceWarning
            pass

    def test_container(self):
        """
        test that runner figures out the proper tasks to run
        """
        import docker

        # may require "Enable default Docker socket" to be set on Docker Desktop for Mac,
        # see https://forums.docker.com/t/docker-errors-dockerexception-error-while-fetching-server-api-version-connection-aborted-filenotfounderror-2-no-such-file-or-directory-error-in-python/135637/3
        client = docker.from_env()
        assert client, "docker not installed?"

        runner = Runner(YamlManifest(manifest))
        # pickled = pickle.dumps(runner.manifest, -1)
        # manifest2 = pickle.loads(pickled)

        run1 = runner.run(JobOptions(check=True, template="container1"))
        # configure (start op shouldn't run since docker_container sets state to started)
        assert len(run1.workDone) == 2, run1.workDone
        tasks = list(run1.workDone.values())
        assert not tasks[1].target.attributes.get("container"), "testing that container property isn't required"
        # print([task.result.outputs for task in tasks])
        assert tasks[1].result and tasks[1].result.outputs
        container = tasks[1].result.outputs.get("container")
        assert container
        self.assertEqual(container["Name"], "/test_docker")
        self.assertEqual(container["State"]["Status"], "exited")
        self.assertEqual(container["Config"]["Image"], "busybox")
        self.assertIn("hello", container["Output"].strip())

        assert tasks[0].status.name == "ok", tasks[0].status
        assert tasks[1].status.name == "ok", tasks[1].status
        assert not run1.unexpectedAbort, run1.unexpectedAbort.get_stack_trace()
        assert tasks[0].target.status.name == "ok", tasks[0].target.status
        assert tasks[1].target.status.name == "ok", tasks[1].target.status

        assert "::container1::container_image" in run1.manifest.manifest.config["changes"][-1]["digestKeys"], run1.manifest.manifest.config["changes"]

        run2 = runner.run(JobOptions(workflow="undeploy", template="container1"))
        # stop op shouldn't be called, just delete
        assert len(run2.workDone) == 1, run2.workDone
        assert not run2.unexpectedAbort, run2.unexpectedAbort.get_stack_trace()
        tasks = list(run2.workDone.values())
        # runner.manifest.dump()
        assert tasks[0].status.name == "ok", tasks[0].status
        assert tasks[0].target.status.name == "absent", tasks[0].target.status

    def test_login(self):
        """
        test that runner figures out the proper tasks to run
        """
        import docker

        client = docker.from_env()
        assert client, "docker not installed?"

        runner = Runner(YamlManifest(manifest))
        # pickled = pickle.dumps(runner.manifest, -1)
        # manifest2 = pickle.loads(pickled)

        run1 = runner.run(JobOptions(instance="test1"))
        assert len(run1.workDone) == 1, run1.workDone
        tasks = list(run1.workDone.values())
        assert run1.rootResource.query("::test1::.artifacts::image::.repository::credential::user") == 'a_user'
        assert run1.rootResource.query("::test1::registry_user") == "a_user"
        assert run1.rootResource.query("::test1::registry_password") == "a_password"
        # docker login will fail because user doesn't exist:
        assert tasks[0].status.name == "error", tasks[0].status
        assert tasks[0].result and tasks[0].result.result, "failed for unexpected reason?"
        self.assertIn("401 Client Error", tasks[0].result.result.get("msg", ""))
        # but the repository and image path will have been created
        self.assertEqual(
            tasks[0].result.outputs.get("image_path"),
            "index.docker.io/repo/image",
        )
        registry = tasks[0].result.outputs.get("registry")
        assert registry and isinstance(registry, toscaparser.repositories.Repository)
        assert not run1.unexpectedAbort, run1.unexpectedAbort.get_stack_trace()

    def test_environment(self):
        src_path = str(Path(__file__).parent / "examples" / "docker-ensemble.yaml")
        jobs = list(
            isolated_lifecycle(
                src_path,
                steps=[Step("plan", Status.ok)],
            )
        )
        env = jobs[0].rootResource.find_instance("container1").attributes['container']['environment']
        assert env == {'FOO': '1', 'BAR': '1', 'PASSWORD': 'test'}

    def test_lifecycle(self):
        # note: this tests dynamically skipping an operation (start) because the previous one (create) 
        # sets the state state
        src_path = str(Path(__file__).parent / "examples" / "docker-ensemble.yaml")
        list(
            isolated_lifecycle(
                src_path,
                steps=DEFAULT_STEPS[1:],
            )
        )
