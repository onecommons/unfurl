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
              service_name: my_service # only applies if container is used
              labels: {} # added to the docker service, see https://kompose.io/user-guide/#labels
              annotations: {} # annotations to add to the metadata section
              ingress_extras: {} # merge into ingress record
              # XXX
              # env: "{{ SELF.env }}" # currently only container.environment is supported
"""

from typing import cast
from toscaparser.elements.portspectype import PortSpec
from .shell import ShellConfigurator
from .k8s import make_pull_secret, mark_sensitive
from ..util import UnfurlTaskError
from ..yamlloader import yaml
from ..merge import merge_dicts
from ..projectpaths import Folders
from ..support import to_kubernetes_label, ContainerImage, Reason
import os
import os.path
from pathlib import Path


TIMEOUT = 180  # default timeout in seconds (3 minutes)


def _get_service(compose: dict, service_name: str = None):
    if service_name:
        return compose["services"][service_name]
    else:
        return list(compose["services"].values())[0]


def _add_labels(service: dict, labels: dict):
    service.setdefault("labels", {}).update(labels)


def render_compose(container: dict, image="", service_name=None):
    service = dict(restart="always")
    service.update({k: v for k, v in container.items() if v is not None})
    if image:
        service["image"] = image
    else:
        image = service["image"]
    if not service_name:
        service_name = container.get("container_name", ContainerImage.split(image)[0])
    return dict(services={to_kubernetes_label(service_name): service})


def get_ingress_extras(task, ingress_extras):
    if ingress_extras and isinstance(ingress_extras, str):
        if ingress_extras.strip():
            try:
                ingress_extras = yaml.load(ingress_extras)
            except Exception:
                raise UnfurlTaskError(
                    task, f"error parsing ingress_extras:\n{ingress_extras}"
                )
        else:
            return None
    return ingress_extras


class KomposeConfigurator(ShellConfigurator):
    _default_cmd = "kompose"
    _default_dryrun_arg = "--dry-run='client'"
    _output_dir = "kompose"

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
            container = task.inputs.get_copy("container") or {}
            compose = render_compose(
                container, task.inputs.get("image"), task.inputs.get("service_name")
            )

        service_name = self._validate(task, compose)
        # XXX can be more than one service
        task.target.attributes["name"] = service_name
        if "version" not in compose:
            # kompose fails without this
            compose["version"] = "3.7"

        # save in secrets folder cuz generated templates can include k8s secrets or sensitive env vars
        cwd = task.set_work_folder(Folders.tasks)
        assert cwd.pending_state
        _, cmd = self._cmd(
            task.inputs.get("command", self._default_cmd), task.inputs.get("keeplines")
        )
        if task.verbose:
            cmd.append("-v")
        main_service = _get_service(compose)
        labels = task.inputs.get("labels") or {}
        for key in list(labels):
            if (
                key.startswith("kompose.service.healthcheck.liveness")
                and "healthcheck" not in main_service
            ):
                # add a default healthcheck
                main_service["healthcheck"] = dict(
                    interval="10s", timeout="10s", retries=3, start_period="30s"
                )
            if key.endswith(".http_get_port") or key.endswith(".tcp_port"):
                if labels[key] is None and main_service.get("ports"):
                    # port isn't specified, set the service's port
                    labels[key] = PortSpec.make(main_service["ports"][0])["source"]
        labels["kompose.image-pull-policy"] = main_service.get(
            "pull_policy", "Always"
        ).title()
        # create the output directory:
        output_dir = cwd.get_current_path(self._output_dir)
        if not os.path.isdir(output_dir):
            os.makedirs(output_dir)

        registry_password = task.inputs.get("registry_password")
        if registry_password:
            pull_secret_name = f"{service_name}-registry-secret"
            registry_url = task.inputs.get("registry_url")
            registry_user = task.inputs.get("registry_user")
            pull_secret = make_pull_secret(
                pull_secret_name, registry_url, registry_user, registry_password
            )
            cwd.write_file(
                pull_secret,
                f"{self._output_dir}/{pull_secret_name}.yaml",
                encoding="utf-8",
            )
            labels["kompose.image-pull-secret"] = pull_secret_name
        expose = task.inputs.get("expose")
        if expose:
            labels["kompose.service.expose"] = expose
        _add_labels(main_service, labels)

        # map files to configmap
        # XXX
        # env-file creates configmap - it'd be nice if these could be secrets instead:
        # replace configMapKeyRef with secretKeyRef
        # replace configmap kind with secret, add type: Opaque
        # kompose.volume.type	: configMap
        # if there's a volume mapping that points to file use --volumes=configMap option,
        # (see https://github.com/kubernetes/kompose/pull/1216)
        task.logger.debug("writing docker-compose:\n%s", compose)
        cwd.write_file(compose, "docker-compose.yml")
        result = self.run_process(cmd + ["convert", "-o", output_dir], cwd=cwd.cwd)
        ingress_extras = get_ingress_extras(
            task, task.inputs.get_copy("ingress_extras")
        )
        if ingress_extras:
            task.logger.debug("setting ingress_extras to:\n%s", ingress_extras)
        if not self._handle_result(task, result, cwd.cwd):
            raise UnfurlTaskError(task, "kompose convert failed")
        definitions = self.save_definitions(task, output_dir, ingress_extras)
        task.target.attributes["definitions"] = definitions
        return definitions

    def save_definitions(self, task, out_path, ingress_extras):
        files = os.listdir(out_path)
        # add each file as a Unfurl k8s resource so unfurl can manage them (in particular, delete them)
        task.logger.verbose(
            "Creating Kubernetes resources from these files: %s", ", ".join(files)
        )
        return {
            Path(filename).stem: _load_resource_file(
                task, out_path, filename, ingress_extras
            )
            for filename in files
        }

    # XXX if updating delete previously created resources that are no longer referenced
    # XXX when updating compare resources, if nothing has yield the restart operation
    # XXX add a delete operation that deletes child resources
    def run(self, task):
        assert task.configSpec.operation in ["configure", "create"]
        # add each file as a Unfurl k8s resource so unfurl can manage them (in particular, delete them)
        definitions = task.target.attributes["definitions"] or {}
        jobRequest, errors = task.update_instances(
            [
                _render_template(task, task.target.name, name, definition["kind"])
                for name, definition in definitions.items()
            ]
        )
        if errors:
            task.logger.error(
                "Errors while creating Kubernetes resources generated by Kompose"
            )
        # XXX:
        # errors = self.process_result_template(task, result.__dict__)
        # success = not errors
        yield self.done(task)

    def can_dry_run(self, task):
        return True


def configure_inputs(kind, timeout):
    timeout = timeout or TIMEOUT
    delay = 10
    configuration = dict(wait=True, wait_timeout=timeout)
    if kind == "Deployment":
        configuration["wait_condition"] = {"status": "True", "type": "Progressing"}
    inputs = {"configuration": configuration}
    if kind == "Ingress":
        inputs["playbook"] = dict(
            register="rs",
            until="rs.result.status.loadBalancer.ingress is defined",
            retries=int(timeout / delay),
            delay=delay,
        )
    return inputs


def _load_resource_file(task, out_path, filename, ingress_extras):
    with open(Path(out_path) / filename) as f:
        definition_str = f.read()
    definition = yaml.load(definition_str)
    assert isinstance(definition, dict)
    if definition["kind"] == "Ingress" and ingress_extras:
        assert isinstance(ingress_extras, dict)
        definition = cast(dict, merge_dicts(definition, ingress_extras))
    annotations = task.inputs.get("annotations")
    if annotations:
        definition.setdefault("metadata", {}).setdefault("annotations", {}).update(
            annotations
        )
    mark_sensitive(task, definition)
    return definition


def _render_template(task, rname: str, name: str, kind: str) -> dict:
    expr = dict(eval=f"::{rname}::definitions::{name}")
    return dict(
        name=name,
        parent="SELF",  # so the host namespace is honored
        template=dict(
            type="unfurl.nodes.K8sRawResource",
            properties=dict(definition=expr),
            interfaces=dict(
                Standard=dict(inputs=configure_inputs(kind, task.configSpec.timeout))
            ),
        ),
    )
