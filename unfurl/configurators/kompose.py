"""
  Run a container or docker compose file on Kubernetes using Kompose

  K8sContainerHost:
    derived_from: tosca:Root
    interfaces:
      Standard:
        operations:
          configure:
            implementation: Kompose
            inputs:
              # generates a docker-compose file for conversion
              container: "{{ SELF.container }}"
              image: "{{ SELF.container_image }}" # overrides the container image
              files:
                # files that aren't named docker-compose.yml are converted to configmaps
                "docker-compose.yml": # overrides "container" and "image"
                    services:
                      app:
                        image: app:latest
                "filename.txt": contents
              registry_url: "{{ SELF.registry_url }}"
              # if set, create a pull secret:
              registry_user: "{{ SELF.registry_user }}"
              registry_password: "{{ SELF.registry_password }}"
              # if set, creates ingress record:
              expose: true # or "foobar.example.com"
              # XXX
              # env: "{{ SELF.env }}" # currently only container.environment is supported
"""

from .shell import ShellConfigurator
from .k8s import make_pull_secret
from ..projectpaths import FilePath, Folders
import os
import os.path
from pathlib import Path
from ..util import UnfurlTaskError
from ..yamlloader import yaml


def _get_service(compose: dict, service_name: str = None):
    if service_name:
        return compose["services"][service_name]
    else:
        return list(compose["services"].values())[0]


def _add_labels(service: dict, labels: dict):
    service.setdefault("labels", {}).update(labels)


def render_compose(container: dict, image="", service_name="app"):
    service = dict(restart="always")
    service.update({k:v for k, v in container.items() if v is not None})
    if image:
        service["image"] = image
    return dict(services={
        service_name: service
    })

class KomposeConfigurator(ShellConfigurator):
    _default_cmd = "kompose"
    _default_dryrun_arg = "--dry-run='client'"
    _output_dir = 'kompose'

    def _validate(self, task, compose):
        services = isinstance(compose, dict) and compose.get("services")
        if not services:
            raise UnfurlTaskError(
                  task,
                  f'docker-compose.yml is invalid: "{compose}"',
                  )
        service_name = list(services)[0]
        service = services[service_name]
        if not isinstance(service, dict) or not service.get("image"):
            raise UnfurlTaskError(
                task,
                f'docker-compose.yml does not specify a container image: "{compose}"',
            )
        return service_name

    # inputs: files, env
    def render(self, task):
        """
        1. Render the docker_compose template if neccessary
        2. Render the kubernetes resources using kompose
        3. create docker-registy pull secret
        """
        if task.inputs.get("files") and task.inputs["files"].get("docker-compose.yml"):
            compose = task.inputs["files"]["docker-compose.yml"]
        else:
            compose = render_compose(task.inputs.get_copy("container") or {}, task.inputs.get("image"))

        service_name = self._validate(task, compose)
        # XXX can be more than one service
        task.target.attributes['name'] = service_name
        if "version" not in compose:
            # kompose fails without this
            compose["version"] = '3'

        # save in secrets folder cuz generated templates can include k8s secrets or sensitive env vars
        cwd = task.set_work_folder(Folders.tasks)
        assert cwd.pending_state
        _, cmd = self._cmd(
            task.inputs.get("command", self._default_cmd), task.inputs.get("keeplines")
        )
        if task.verbose:
            cmd.append('-v')
        service = _get_service(compose)
        labels = {"kompose.image-pull-policy": service.get("pull_policy", "Always").title()}

        # create the output directory:
        output_dir = cwd.get_current_path(self._output_dir)
        if not os.path.isdir(output_dir):
            os.makedirs(output_dir)

        registry_password = task.inputs.get("registry_password")
        if registry_password:
            pull_secret_name = f"{service_name}-registry-secret"
            registry_url = task.inputs.get("registry_url")
            registry_user = task.inputs.get("registry_user")
            pull_secret = make_pull_secret(pull_secret_name, registry_url, registry_user, registry_password)
            cwd.write_file(pull_secret, f"{self._output_dir}/{pull_secret_name}.yaml", encoding='utf-8')
            labels["kompose.image-pull-secret"] = pull_secret_name
        expose = task.inputs.get("expose")
        if expose:
            labels["kompose.service.expose"] = expose
        # XXX  (unreleased) kompose.service.expose.ingress-class-name see https://github.com/kubernetes/kompose/pull/1486
        _add_labels(service, labels)

        # map files to configmap
        # XXX
        # env-file creates configmap - it'd be nice if these could be secrets instead:
        # replace configMapKeyRef with secretKeyRef
        # replace configmap kind with secret, add type: Opaque
        # https://github.com/kubernetes/kompose/pull/1216
        # kompose.volume.type	: configMap
        task.logger.debug("writing docker-compose:\n %s", compose)
        cwd.write_file(compose, "docker-compose.yml")
        result = self.run_process(cmd + ["convert", "-o", output_dir], cwd=cwd.cwd)
        if not self._handle_result(task, result, cwd.cwd):
            raise UnfurlTaskError(task, "kompose failed")

    # XXX if updating should either update or delete previously created resources
    # XXX when updating compare resources, if nothing has yield the restart operation
    # XXX add a delete operation that deletes child resources
    def run(self, task):
        assert task.configSpec.operation in ["configure", "create"]
        cwd = task.get_work_folder()
        out_path = cwd.get_current_path(self._output_dir)
        files = os.listdir(out_path)
        # add each file as a Unfurl k8s resource so unfurl can manage them (in particular, delete them)
        task.logger.verbose("Creating Kubernetes resources from these files: %s", ", ".join(files))
        jobRequest, errors = task.update_instances([
            _load_resource_file(out_path, filename) for filename in files
        ])
        if errors:
            task.logger.error("Errors while creating Kubernetes resources generated by Kompose")
        # XXX:
        # errors = self.process_result_template(task, result.__dict__)
        # success = not errors
        yield self.done(task)

    def can_dry_run(self, task):
        return True


def configure_resource(definition):
    configuration = dict(wait=True)
    if definition["kind"] == "Deployment":
        configuration["wait_condition"] = {"status": "True", "type": "Progressing"}
    return configuration


def _load_resource_file(out_path, filename):
    with open(Path(out_path) / filename) as f:
        definition = f.read()
    definition = yaml.load(definition)
    return dict(
        name=Path(filename).stem,
        parent="SELF",  # so the host namespace is honored
        template=dict(type="unfurl.nodes.K8sRawResource",
                        properties=dict(definition=definition),
                        interfaces=dict(
                            Standard=dict(inputs={"configuration": configure_resource(definition)})
                        ),
                      )
    )
