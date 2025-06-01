# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
import re
import os
from base64 import b64encode, b64decode
from typing import Any, Dict, List, Mapping, Union, cast

from ..projectpaths import get_path
from ..configurator import Configurator, TaskView, ConfiguratorResult
from ..support import Status, Priority, to_kubernetes_label, find_connection
from ..runtime import (
    EntityInstance,
    NodeInstance,
    RelationshipInstance,
    CapabilityInstance,
)
from ..util import UnfurlTaskError, wrap_sensitive_value, sensitive_dict
from .ansible import AnsibleConfigurator
from ..yamlloader import yaml
from ..eval import Ref, RefContext, set_eval_func, map_value
from .k8s_ansible import get_api_client
import subprocess
import json


def _get_connection_config(
    instance: Union[RelationshipInstance, CapabilityInstance],
) -> Dict[str, Any]:
    "Given an endpoint capability or a connection relationship, return a dictionary of connection settings."
    # this will be merged into the generated ansible playbook
    # https://docs.ansible.com/ansible/latest/collections/kubernetes/core/k8s_module.html
    #  for connection settings
    # https://github.com/ansible-collections/kubernetes.core/blob/7f7008fecc9e5d16340e9b0bff510b7cde2f2cfd/plugins/connection/kubectl.py
    if not instance:
        return {}
    connection: Dict[str, Union[str, bool]] = {}
    if isinstance(instance, RelationshipInstance):
        connect: Mapping = instance.attributes
        # the parent of a relationship will be the capability it connects to
        # or root if relationship is a default connection
        if instance.parent and instance.parent is not instance.root:
            endpoint: Mapping = instance.parent.attributes
        else:
            endpoint = {}
    else:  # assume it's an endpoint capability
        endpoint = instance.attributes
        connect = {}

    # endpoint capability only
    protocol = endpoint.get("protocol")
    ip_address = endpoint.get("ip_address")
    port = endpoint.get("port")
    if ip_address:
        if port:
            connection["host"] = f"{protocol}://{ip_address}:{port}"
        else:
            connection["host"] = f"{protocol}://{ip_address}"

    # connection only:
    api_server = connect.get("api_server")
    if api_server:
        if "//" in api_server:
            connection["host"] = api_server
        else:
            connection["host"] = f"{connect['protocol']}://{api_server}"

    map1 = {
        "KUBECONFIG": "kubeconfig",
        "context": "context",
        "namespace": "namespace",
        "as": "impersonate_user",
        "as-group": "impersonate_groups",
    }
    # relationship overrides capability
    for attributes in [endpoint, connect]:
        for key, value in map1.items():
            if key in attributes:
                connection[value] = attributes[key]

        if "insecure" in attributes:
            connection["validate_certs"] = not attributes["insecure"]

        credential = attributes.get("credential")
        if credential:
            if credential.get("token_type") in ["api_key", "password"]:
                connection[credential["token_type"]] = wrap_sensitive_value(
                    credential["token"]
                )
            if "user" in credential:
                connection["username"] = credential["user"]
            if not attributes.get("insecure") and "keys" in credential:
                # ["ca_cert", "cert_file", "key_file"]
                connection.update(credential["keys"])

        if attributes.get("token"):
            connection["api_key"] = attributes["token"]

        # K8sAnsibleMixin needs you to explicitly omit certs if you set insecure to true
        if (
            not attributes.get("insecure")
            and "ca_cert" not in connection
            and attributes.get("cluster_ca_certificate")
        ):
            connection["ca_cert"] = attributes["cluster_ca_certificate_file"]
    return connection


CONNECTION_OPTIONS = {
    "namespace": "-n",
    "kubeconfig": "--kubeconfig",
    "context": "--context",
    "host": "--server",
    "username": "--username",
    "password": "--password",
    "client_cert": "--client-certificate",
    "client_key": "--client-key",
    "ca_cert": "--certificate-authority",
    "api_key": "--token",
    "as": "--as",
}


def _get_connection(ctx: RefContext) -> dict:
    # get the cluster that the target resource is hosted on
    ctx = ctx.copy(cast(EntityInstance, ctx.currentResource).owner)
    cluster = Ref(".hosted_on::[.type=unfurl.nodes.K8sCluster]").resolve_one(ctx)
    relation = "unfurl.relationships.ConnectsTo.K8sCluster"
    if cluster:
        rel_instance = find_connection(ctx, cast(NodeInstance, cluster), relation)
    else:
        relationships = cast(
            EntityInstance, ctx.currentResource
        ).get_default_relationships(relation)
        if relationships:
            rel_instance = relationships[0]
        else:
            rel_instance = None
    if rel_instance:
        config = _get_connection_config(rel_instance)
    else:
        config = {}
    empty_config = not config
    # check if we are in a namespace
    namespace = Ref(".hosted_on::[.type=unfurl.nodes.K8sNamespace]").resolve_one(ctx)
    if namespace:
        config["namespace"] = cast(NodeInstance, namespace).attributes["name"]
    if ctx.task:
        if empty_config:
            ctx.task.logger.verbose(
                "Empty k8s config, falling back to current kube context (if present), searched connection %s, cluster %s, namespace %s for %s",
                rel_instance,
                cluster,
                namespace,
                ctx.currentResource,
            )
        else:
            ctx.task.logger.debug(
                "Found k8s connection: %s from connection %s, cluster %s, namespace %s for %s",
                config,
                rel_instance,
                cluster,
                namespace,
                ctx.currentResource,
            )
    return config


set_eval_func(
    "kubernetes_current_namespace",
    lambda args, ctx: _get_connection(ctx).get("namespace"),
)


def get_kubectl_args(ctx: RefContext):
    connection = _get_connection(ctx)
    options = []
    for key, value in connection.items():
        if value and key in CONNECTION_OPTIONS:
            options.append(CONNECTION_OPTIONS[key])
            options.append(value)
    if "validate_certs" in connection and not connection["validate_certs"]:
        options.append("--insecure-skip-tls-verify")
    groups = connection.get("as-group") or []
    for group in groups:
        options.append("--as-group")
        options.append(group)
    return options


def kubectl(cmd, ctx: RefContext):
    cmd = map_value(cmd, ctx)
    args = get_kubectl_args(ctx)
    if not isinstance(cmd, list):
        cmd = cmd.split()
    full_cmd = ["kubectl"] + args + cmd
    if ctx.kw.get("execute"):
        return subprocess.run(full_cmd, capture_output=True).stdout
    else:
        return full_cmd


set_eval_func("kubectl", kubectl)


def make_pull_secret(name, hostname, username, password) -> str:
    data = dict(
        auths={
            hostname: dict(
                username=username,
                password=password,
                auth=b64encode(f"{username}:{password}".encode()).decode(),
            )
        }
    )
    return f"""
    apiVersion: v1
    kind: Secret
    metadata:
      name: {name}
    type: kubernetes.io/dockerconfigjson
    data:
      .dockerconfigjson: {b64encode(json.dumps(data).encode()).decode()}
    """


class ClusterConfigurator(Configurator):
    @staticmethod
    def _get_host(connectionConfig):
        client = get_api_client(**connectionConfig)
        url = client.configuration.host
        if url:
            return re.sub("^https?://", "", url)
        return url

    def can_run(self, task: TaskView):
        if task.configSpec.operation not in ["check", "discover"]:
            return "Configurator can't perform this operation (only supports check and discover)"
        return True

    def can_dry_run(self, task):
        return True

    def run(self, task: TaskView):
        cluster = task.target
        # the endpoint on the cluster will have connection info
        instances = cast(NodeInstance, cluster).get_capabilities("endpoint")
        if instances:
            connectionConfig = _get_connection_config(instances[0])
        else:
            connectionConfig = {}
        if not instances or not connectionConfig:
            # no endpoint, look for a default connection for this cluster
            rel_instances = cluster.get_default_relationships(
                "unfurl.relationships.ConnectsTo.K8sCluster"
            )
            if rel_instances:
                connectionConfig = _get_connection_config(rel_instances[0])
        else:
            rel_instances = []

        if cluster.attributes.get("api_server"):
            # api_server was set before, don't let it change --
            # if the connection has a different host, its pointing at a different cluster
            connectionConfig["host"] = "https://" + cluster.attributes["api_server"]

        task.logger.debug(
            "checking cluster connection %s from %s",
            connectionConfig,
            rel_instances or instances,
        )
        if task.dry_run:
            yield task.done(True, False, Status.ok)
            return
        try:
            # try connect and save the resolved host
            cluster.attributes["api_server"] = self._get_host(connectionConfig)
        except Exception:
            yield task.done(
                False,
                False,
                captureException="error while trying to establish connection to cluster",
            )
        else:
            # we aren't modifying this cluster but we do want to assert that its ok
            yield task.done(True, False, Status.ok)


class ConnectionConfigurator(ClusterConfigurator):
    def should_run(self, task):
        return Priority.critical  # abort if unable to connect to the cluster

    def run(self, task: TaskView):
        connection = task.target
        assert isinstance(connection, RelationshipInstance)
        connectionConfig = _get_connection_config(connection)
        try:
            # try connect and save the resolved host
            task.logger.debug("k8s connection configuration: %s", connectionConfig)
            connection.attributes["api_server"] = self._get_host(connectionConfig)
        except Exception:
            if task.dry_run:
                task.logger.warning(
                    "ignoring k8s connection check failure during dry run: %s",
                    connectionConfig,
                )
                yield task.done(True, False, Status.ok)
            else:
                yield task.done(
                    False,
                    False,
                    captureException="error while trying to establish connection to cluster",
                )
        else:
            # we aren't modifying this connection but we do want to assert that its ok
            yield task.done(True, False, Status.ok)


def mark_sensitive(task, resource):
    data = resource.get("kind") == "Secret" and resource.get("data")
    if data:
        resource["data"] = {k: task.sensitive(v) for k, v in data.items()}

    obj = resource
    for key in "spec/template/spec".split("/"):
        obj = obj.get(key)
        if not obj:
            break
        if key == "spec":
            for ckey in "containers", "initContainers", "ephemeralContainers":
                containers = obj.get(ckey)
                if containers:
                    for container in containers:
                        for env in container.get("env") or []:
                            if "value" in env:
                                env["value"] = task.sensitive(env["value"])


ClusterScoped_Kinds = [
    "ClusterRole",
    "ClusterRoleBinding",
    "CustomResourceDefinition",
    "MutatingWebhookConfiguration",
    "ValidatingWebhookConfiguration",
    "Node",
    "PersistentVolume",
    "PriorityClass",
    "StorageClass",
    "VolumeSnapshotClass",
    "RuntimeClass",
    "APIService",
    "ClusterIssuer",
]


class ResourceConfigurator(AnsibleConfigurator):
    @classmethod
    def set_config_spec_args(cls, kw: dict, template):
        artifact = template.find_or_create_artifact("kubernetes.core", predefined=True)
        if artifact:
            kw["dependencies"].append(artifact)
        return kw

    def get_generator(self, task: TaskView):
        if task.dry_run:
            return self.dry_run(task)
        else:
            return self.run(task)

    def dry_run(self, task: TaskView):
        task.logger.info("dry run: generating playbook:\n%s", self.find_playbook(task))
        yield task.done(True)

    def make_secret(self, data: Dict[str, Any], type: str = "Opaque") -> Dict[str, Any]:
        return dict(
            type=type,
            apiVersion="v1",
            kind="Secret",
            data=sensitive_dict({
                k: b64encode(str(v).encode()).decode()
                for k, v in data.items()
                if v is not None
            }),
        )

    def get_definition(self, task: TaskView) -> List[Dict[str, Any]]:
        if task.target.template.is_compatible_type("unfurl.nodes.K8sNamespace"):
            return [dict(apiVersion="v1", kind="Namespace")]

        secret = task.target.template.is_compatible_type(
            "unfurl.nodes.K8sSecretResource"
        )
        if secret and isinstance(task.target.attributes.get("data"), Mapping):
            return [
                self.make_secret(
                    task.target.attributes["data"], task.target.attributes["type"]
                )
            ]

        # get copy so subsequent modifications don't affect the definition
        if "definition" in task.target.attributes:
            definition = task.target.attributes.get_copy("definition") or {}
        elif "src" in task.target.attributes:
            src = task.target.attributes["src"]
            if not os.path.isabs(src):
                src = get_path(task.inputs.context, src, "src")
            with open(src) as f:
                definition = f.read()
        else:
            definition = task.target.attributes.get_copy("apiResource") or {}

        if not definition and secret and task.configSpec.operation == "discover":
            name = task.target.attributes.get("name") or to_kubernetes_label(
                task.target.name
            )
            return [dict(apiVersion="v1", kind="Secret", metadata=dict(name=name))]

        if isinstance(definition, str):
            definition = list(yaml.load_all(definition))
        elif isinstance(definition, dict):
            return [definition]
        return definition

    def update_metadata(self, definition: Dict[str, Any], task: TaskView, namespace):
        "Add name and namespace to metadata if missing from metadata."
        md = definition.setdefault("metadata", {})
        name = task.target.attributes.get("name") or to_kubernetes_label(
            task.target.name
        )
        if "name" in md and md["name"] != name:
            name = task.target.attributes["name"] = md["name"]
        elif task.configSpec.operation != "discover":
            md["name"] = name

        if (
            namespace
            and "namespace" not in md
            and definition["kind"] not in ClusterScoped_Kinds
        ):
            if namespace:
                md["namespace"] = namespace
            else:
                task.logger.warning(f"No kubernetes namespace set for {name}.")
        # else: error if namespace mismatch?

    def _make_check(self, connectionConfig, definition, extra_configuration):
        "Make a k8s_info play for checking the existence of a resource."
        moduleSpec = connectionConfig.copy()
        moduleSpec["kind"] = definition.get("kind", "")
        if "name" in definition["metadata"]:
            moduleSpec["name"] = definition["metadata"]["name"]
        if moduleSpec["kind"] in ClusterScoped_Kinds:
            moduleSpec.pop("namespace", None)
        elif "namespace" in definition["metadata"]:
            moduleSpec["namespace"] = definition["metadata"]["namespace"]
        if extra_configuration:
            moduleSpec.update(extra_configuration)
        return [{"kubernetes.core.k8s_info": moduleSpec}]

    def _find_host(self, task: TaskView):  # override
        if task.configSpec.operation_host:  # explicitly set
            return super()._find_host(task)
        return None, None  # use local connection, don't search for hosts

    def find_playbook(self, task: TaskView):
        # this is called by AnsibleConfigurator.get_playbook()
        definitions = self.get_definition(task)
        playbook = []
        for definition in definitions:
            if definition:
                playbook.extend(self._find_playbook(definition, task))
        if not playbook:
            raise UnfurlTaskError(task, "no Kubernetes resource definitions found")
        return playbook

    def _find_playbook(self, definition, task: TaskView):
        kind = definition.get("kind")
        if not kind:
            raise UnfurlTaskError(
                task, 'invalid Kubernetes resource definition, "kind" is missing'
            )
        if kind == "Secret" and "data" in definition:
            definition["data"] = wrap_sensitive_value(definition["data"])

        connectionConfig = _get_connection(task.inputs.context)
        self.update_metadata(definition, task, connectionConfig.get("namespace"))
        extra_configuration = task.inputs.get("configuration")
        extra_playbook = task.inputs.get("playbook")

        if task.configSpec.operation in ["check", "discover"]:
            labels = definition["metadata"].get("labels")
            if task.configSpec.operation == "discover" and labels:
                selectors = [f"{key} = {value}" for key, value in labels.items()]
                if not extra_configuration:
                    extra_configuration = dict(label_selectors=selectors)
                else:
                    extra_configuration.setdefault("label_selectors", []).extend(
                        selectors
                    )
            return self._make_check(connectionConfig, definition, extra_configuration)

        delete = task.configSpec.operation in ["Standard.delete", "delete"]
        state = "absent" if delete else "present"
        moduleSpec = dict(state=state, **connectionConfig)
        moduleSpec["resource_definition"] = definition

        if extra_configuration:
            moduleSpec.update(extra_configuration)

        playbook = [{"kubernetes.core.k8s": moduleSpec}]
        if extra_playbook:
            playbook[0].update(extra_playbook)
        if definition and (
            task.target.created is None
            and task.target.status in [Status.pending, Status.unknown]
        ):
            # we don't want delete resources that already exist (especially namespaces!)
            # so the first time we try to create the resource check if it exists first,
            # that way we know whether to mark resources as created by us or not
            task.logger.trace(
                "adding k8s resource existence check for %s", task.target.name
            )
            # don't pass the same extra_configuration to make_check
            return self._make_check(connectionConfig, definition, None) + playbook
        else:
            return playbook

    def process_result(
        self, task: TaskView, result: ConfiguratorResult
    ) -> ConfiguratorResult:
        # overrides super().process_result
        # this is only called if the ansible playbook succeeded
        assert isinstance(result.result, dict)
        resource_result = result.result.get("result")
        resources = result.result.get("resources")
        if resources:  # this is set when we have a k8s_info playbook task
            resource = resources[0]
            task.logger.verbose("found existing k8s resource for %s", task.target.name)
            if task.target.created is None:
                # already existed so set this as not created by us
                task.target.created = False
        else:
            resource = resource_result
        task.target.attributes["apiResource"] = resource
        if resource:
            # for now, mark all env values sensitive
            # XXX only mark env values that were marked sensitive in the definition
            mark_sensitive(task, resource)
            if task.configSpec.operation in ["check", "discover"]:
                states = dict(
                    Active=Status.ok,
                    Terminating=Status.absent,
                    Pending=Status.pending,
                    Running=Status.ok,
                    Succeeded=Status.absent,
                    Failed=Status.error,
                    Unknown=Status.unknown,
                )
                if "phase" in resource.get("status", {}):
                    status = resource["status"].get("phase", "Unknown")
                    result.status = states.get(status, Status.unknown)
                else:
                    result.status = Status.ok
                    if task.target.template.is_compatible_type(
                        "unfurl.nodes.K8sSecretResource"
                    ) and isinstance(resource.get("data"), dict):

                        def decode(v):
                            try:
                                return b64decode(v).decode()
                            except UnicodeError:
                                return v  # keep binary data encoded

                        task.target.attributes["data"] = sensitive_dict({
                            k: decode(v) for k, v in resource["data"].items()
                        })
                        task.target.attributes["type"] = resource.get("type")

        elif result.status is None:  # don't change if status already set to error
            result.status = Status.absent
        return result

    def get_result_keys(self, task, results):
        # save first time even if it hasn't changed
        return ["resources", "result", "duration"]  # also "method", "diff", invocation
