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
                # TODO: files that aren't named docker-compose.yml are converted to configmaps
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
              env: {} # merged with container.environment
              overlays: {} # merged with resources
              init_containers: [] # run to completion before the container, as the pod's initContainers
Note overlays and extras won't be in the rendered kompose files but applied when createing the kubernetes resources to deploy

"""

from typing import cast, Union, Optional, TYPE_CHECKING, Dict, Any, List, Tuple
from toscaparser.elements.portspectype import PortSpec
from ..tosca_plugins.functions import to_dns_label
from ..result import serialize_value
from ..configurator import TaskView
from ..planrequests import ConfigurationSpecKeywords
from ..spec import EntitySpec
from .shell import ShellConfigurator, ShellInputs
from .k8s import make_pull_secret, mark_sensitive, ClusterScoped_Kinds
from ..util import UnfurlTaskError, escape_chars, which
from ..yamlloader import yaml
from ..merge import merge_dicts
from ..projectpaths import Folders
from ..support import ContainerImage
from ..runtime import EntityInstance
import os
import os.path
import shlex
from pathlib import Path


TIMEOUT = int(os.getenv("UNFURL_KOMPOSE_TIMEOUT") or 180)  # default timeout in seconds (3 minutes)
NAMEMAX = 52  # max service name length
# a compose pull_policy, or a Kubernetes imagePullPolicy, by lowercased name
PULL_POLICIES = {
    "always": "Always",
    "never": "Never",
    "missing": "IfNotPresent",
    "if_not_present": "IfNotPresent",
    "ifnotpresent": "IfNotPresent",
}

if TYPE_CHECKING:
    from .templates.docker import unfurl_datatypes_DockerContainer

class KomposeInputs(ShellInputs):
    """
    Use kompose to render a docker-compose template to kubernetes resources

    See `kompose convert` for more information on the conversion process.

    The following inputs are supported:

    :param files: A dictionary of files to write out to the working directory.
      Each key is the filename and the value is a dictionary with a single key
      "contents" that contains the contents of the file.
    :param container: A `unfurl.nodes.Container.Application.Docker` instance
      that provides the image and other container settings.
    :param image: The name of the container image to use.
    :param registry_url: The url of the registry to pull the container image from.
    :param registry_user: The username to use when pulling the container image.
    :param registry_password: The password to use when pulling the container image.
    :param labels: A dictionary of labels to add to the kubernetes service.
    :param annotations: A dictionary of annotations to add to the kubernetes service.
    :param expose: If true, create a kubernetes ingress record. If a string,
      use that string as the host name in the ingress record.
    :param ingress_extras: Additional configuration for the ingress record.
    :param env: A dictionary of environment variables to add to the container.
    :param init_containers: Containers (in the same format as ``container``) to run to completion,
      in order, before the container starts. They are added to the workload as ``initContainers``
      and can only mount volumes that ``container`` also mounts.
    """
    files: Union[None, Dict[str, Dict[str, Any]]] = None
    container: Union[None, "unfurl_datatypes_DockerContainer"] = None
    image: Optional[str] = None
    service_name: Optional[str] = None
    registry_url: Optional[str] = None
    registry_user: Optional[str] = None
    registry_password: Optional[str] = None
    labels: Union[None, Dict[str, str]] = None
    annotations: Union[None, Dict[str, Any]] = None
    expose: Union[bool, str, None] = None
    ingress_extras: Union[None, Dict[str, Any]] = None
    overlays: Union[None, Dict[str, Dict[str, Any]]] = None
    env: Union[None, Dict[str, str]] = None
    init_containers: Union[None, List["unfurl_datatypes_DockerContainer"]] = None

def _get_service(compose: dict, service_name: Optional[str] = None) -> dict:
    if service_name:
        return compose["services"][service_name]
    else:
        return list(compose["services"].values())[0]


def _add_labels(service: dict, labels: dict):
    service.setdefault("labels", {}).update(labels)

def _named_volumes(volumes: List[Union[str, dict]]) -> List[str]:
    """Return the named volume sources in compose ``volumes`` (not host paths or anonymous volumes)."""
    names = []
    for volume in volumes:
        if isinstance(volume, dict):
            source = volume.get("source") if volume.get("type") == "volume" else None
        else:
            source, sep, _ = volume.partition(":")
            if not sep:
                source = None
        if source and source[0] not in "/.~" and source not in names:
            names.append(source)
    return names



def render_compose(
    container: dict,
    image="",
    service_name: Optional[str] = None,
    env: Optional[dict] = None,
):
    service: Dict[str, Any] = dict(restart="always")
    service.update({k: escape_chars(v) for k, v in container.items() if v is not None})
    if image:
        service["image"] = image
    else:
        image = service.get("image", "")
    if env:
        service.setdefault("environment", {}).update(escape_chars(env))
    if not service_name:
        service_name = container.get("container_name") or ContainerImage.split(image)[0]
    # without an image the service may be unnamed; KomposeConfigurator._validate rejects it
    assert service_name or not image
    # service names are more constrained than generic kubernetes labels, treat as dns host name
    # the max is 63 but give kompose some room to append to the name
    compose = dict(
        services={cast(str, to_dns_label(service_name, max=NAMEMAX)): service}
    )
    named_volumes = _named_volumes(service.get("volumes") or [])
    if named_volumes:
        compose["volumes"] = {name: {} for name in named_volumes}
    return compose


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

    @classmethod
    def set_config_spec_args(
        cls, kw: ConfigurationSpecKeywords, template: EntitySpec
    ) -> ConfigurationSpecKeywords:
        if not which("kompose"):
            artifact = template.find_or_create_artifact("kompose", predefined=True)
            if artifact:
                kw["dependencies"].append(artifact)
        return kw

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
        if service_name != to_dns_label(service_name, max=NAMEMAX):
            raise UnfurlTaskError(
                task,
                f'malformed service name: "{service_name}": must not exceed {NAMEMAX} characters and match [a-z0-9]([-a-z0-9]*[a-z0-9])?',
            )
        return service_name

    def render(self, task: TaskView):
        """
        1. Render the docker_compose template if necessary
        2. Render the kubernetes resources using kompose
        3. create docker-registry pull secret
        """
        if task.inputs.get("files") and task.inputs["files"].get("docker-compose.yml"):
            compose = task.inputs["files"]["docker-compose.yml"]
            if isinstance(compose, str):
                compose = yaml.load(compose)
            assert isinstance(compose, dict)
        else:
            container = task.inputs.get_copy("container") or {}
            assert isinstance(container, dict), type(container)
            compose = render_compose(
                container,
                task.inputs.get_copy("image"),
                task.inputs.get_copy("service_name"),
                task.inputs.get_copy("env"),
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
        # kompose rejects compose's pull_policy key, so it's set with kompose's label
        pull_policy = str(main_service.pop("pull_policy", None) or "always")
        if pull_policy.lower() not in PULL_POLICIES:
            raise UnfurlTaskError(
                task,
                f'pull_policy "{pull_policy}" isn\'t supported on Kubernetes: use one of always, never or missing',
            )
        labels["kompose.image-pull-policy"] = PULL_POLICIES[pull_policy.lower()]
        # create the output directory:
        output_dir = cwd.get_current_path(self._output_dir)
        if not os.path.isdir(output_dir):
            os.makedirs(output_dir)

        registry_password = task.inputs.get("registry_password")
        if registry_password:
            pull_secret_name = f"{service_name}-rgstryscrt"
            registry_url = task.inputs.get("registry_url")
            # XXX validate registry_url, shouldn't be a full url
            registry_user = task.inputs.get("registry_user")
            pull_secret = make_pull_secret(
                pull_secret_name,
                registry_url,
                registry_user or "",
                registry_password or "",
            )
            cwd.write_file(
                pull_secret,
                f"{self._output_dir}/{pull_secret_name}.yaml",
                encoding="utf-8",
            )
            labels["kompose.image-pull-secret"] = pull_secret_name
        expose = task.inputs.get("expose")
        if expose:
            labels["kompose.service.expose"] = (
                "true" if isinstance(expose, bool) else expose
            )
        _add_labels(main_service, escape_chars(labels))

        # map "files" to configmap
        # XXX
        # env-file creates configmap - it'd be nice if these could be secrets instead:
        # replace configMapKeyRef with secretKeyRef
        # replace configmap kind with secret, add type: Opaque
        # kompose.volume.type	: configMap
        # if there's a volume mapping that points to file use --volumes=configMap option,
        # (see https://github.com/kubernetes/kompose/pull/1216)
        composed = serialize_value(compose, resolveExternal=True)
        task.logger.debug("writing docker-compose:\n%s", composed)
        cwd.write_file(composed, "docker-compose.yml")
        result = self.run_process(cmd + ["convert", "-o", output_dir], cwd=cwd.cwd)
        ingress_extras = get_ingress_extras(
            task, task.inputs.get_copy("ingress_extras")
        )
        extras = task.inputs.get_copy("overlays") or {}
        if ingress_extras:
            extras["Ingress"] = ingress_extras
        if extras:
            task.logger.debug("setting extras to:\n%s", extras)
        if not self._handle_result(task, result, cwd.cwd):
            raise UnfurlTaskError(task, "kompose convert failed")
        init_containers = task.inputs.get_copy("init_containers") or []
        # init container volumes aren't compose syntax, so compare them with the unescaped paths
        volumes = [
            escape_chars(v, "$$", "$") for v in main_service.get("volumes") or []
        ]
        definitions = self.save_definitions(
            task, output_dir, extras, init_containers, volumes
        )
        task.target.attributes["definitions"] = definitions
        return definitions

    def save_definitions(
        self,
        task,
        out_path,
        extras,
        init_containers: Optional[List[dict]] = None,
        volumes: Optional[List[str]] = None,
    ):
        files = os.listdir(out_path)
        # add each file as a Unfurl k8s resource so unfurl can manage them (in particular, delete them)
        task.logger.verbose(
            "Creating Kubernetes resources from these files: %s", ", ".join(files)
        )
        return {
            Path(filename).stem: _load_resource_file(
                task, out_path, filename, extras, init_containers, volumes
            )
            for filename in files
        }

    # XXX if updating delete previously created resources that are no longer referenced
    # XXX when updating compare resources, if nothing has yield the restart operation
    # XXX add a delete operation that deletes child resources
    def run(self, task: TaskView):
        assert task.configSpec.operation in ["configure", "create"]
        # add each file as a Unfurl k8s resource so unfurl can manage them (in particular, delete them)
        definitions = task.target.attributes["definitions"] or {}
        namespace = task.target.query(".hosted_on::[.type=unfurl.nodes.K8sNamespace]")
        jobRequest, errors = task.update_instances(
            [
                _render_template(task, task.target.name, name, definition["kind"], namespace)
                for name, definition in definitions.items()
            ]
        )
        if errors:
            task.logger.error(
                "Errors while creating Kubernetes resources generated by Kompose"
            )
        task.logger.verbose("Scheduling generated Kubernetes resources for deployment in namespace %s", namespace)
        # XXX:
        # errors = self.process_result_template(task, result.__dict__)
        # success = not errors
        yield self.done(task)

    def can_dry_run(self, task):
        return True


def configure_inputs(kind, timeout):
    timeout = timeout or TIMEOUT
    delay = 10
    if timeout:
        configuration = dict(wait=True, wait_timeout=timeout)
    else:
        configuration = {}
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


WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet")


def _split_volume(task: TaskView, volume: Any) -> Tuple[str, str, bool]:
    if not isinstance(volume, str) or volume.count(":") not in (1, 2):
        raise UnfurlTaskError(task, f'unsupported volume for an init container: "{volume}"')
    source, target, *mode = volume.split(":")
    return source, target, mode == ["ro"]


def _render_init_containers(
    task: TaskView, init_containers: List[dict], volumes: List[str], pod_spec: dict
) -> List[dict]:
    """Convert compose-style container specs to Kubernetes initContainers that share the pod's volumes."""
    mount_names = {
        mount["mountPath"]: mount["name"]
        for mount in pod_spec["containers"][0].get("volumeMounts") or []
    }
    claims = {}
    for volume in volumes:
        if isinstance(volume, str) and volume.count(":") in (1, 2):
            source, target, _ = _split_volume(task, volume)
            if target in mount_names:
                claims[source] = mount_names[target]
    rendered = []
    for index, init in enumerate(init_containers):
        if init.get("ports") or init.get("expose"):
            raise UnfurlTaskError(task, "init containers can not publish ports")
        if not init.get("image"):
            raise UnfurlTaskError(task, "init containers must specify an image")
        container: Dict[str, Any] = dict(
            name=to_dns_label(init.get("container_name") or f"init-{index}"),
            image=init["image"],
        )
        if init.get("entrypoint"):
            container["command"] = list(init["entrypoint"])
        command = init.get("command")
        if command:
            container["args"] = shlex.split(command) if isinstance(command, str) else list(command)
        env = init.get("environment") or {}
        if env:
            container["env"] = [
                dict(name=name, value=str(value))
                for name, value in env.items()
                if value is not None
            ]
        if init.get("user"):
            uid, _, gid = str(init["user"]).partition(":")
            if not uid.isdigit() or (gid and not gid.isdigit()):
                raise UnfurlTaskError(
                    task, f'init container user must be numeric on Kubernetes: "{init["user"]}"'
                )
            context = dict(runAsUser=int(uid))
            if gid:
                context["runAsGroup"] = int(gid)
            container["securityContext"] = context
        mounts = []
        for volume in init.get("volumes") or []:
            source, target, read_only = _split_volume(task, volume)
            if source not in claims:
                raise UnfurlTaskError(
                    task,
                    f'init container volume "{volume}" must also be mounted by the container on Kubernetes',
                )
            mount: Dict[str, Any] = dict(mountPath=target, name=claims[source])
            if read_only:
                mount["readOnly"] = True
            mounts.append(mount)
        if mounts:
            container["volumeMounts"] = mounts
        rendered.append(container)
    return rendered


def _load_resource_file(
    task,
    out_path,
    filename,
    extras: Dict[str, Any],
    init_containers: Optional[List[dict]] = None,
    volumes: Optional[List[str]] = None,
):
    with open(Path(out_path) / filename) as f:
        definition_str = f.read()
    definition = yaml.load(definition_str)
    assert isinstance(definition, dict)
    if init_containers and definition["kind"] in WORKLOAD_KINDS:
        pod_spec = definition["spec"]["template"]["spec"]
        pod_spec["initContainers"] = _render_init_containers(
            task, init_containers, volumes or [], pod_spec
        )
    if extras and definition["kind"] in extras:
        definition = cast(dict, merge_dicts(definition, extras[definition["kind"]]))
    annotations = task.inputs.get("annotations")
    if annotations:
        definition.setdefault("metadata", {}).setdefault("annotations", {}).update(
            annotations
        )
    mark_sensitive(task, definition)
    return definition


def _render_template(task: TaskView, rname: str, name: str, kind: str, namespace: Optional[EntityInstance]) -> dict:
    expr = dict(eval=f"::{rname}::definitions::{name}")
    if kind in ClusterScoped_Kinds:
        type="unfurl.nodes.K8sRawResource"
        namespace = None
    elif kind == "Secret":
        type="unfurl.nodes.K8sSecretResource"
    else:
        type ="unfurl.nodes.K8sResource"

    spec = dict(
        name=name,
        template=dict(
            type=type,
            properties=dict(definition=expr),
            interfaces=dict(
                Standard=dict(inputs=configure_inputs(kind, task.configSpec.timeout))
            ),
        ),
    )
    if namespace:
        cast(dict, spec["template"])["requirements"] = [{"host": namespace.name}]
    else:
        spec["parent"] ="SELF"
    return spec