# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
import re
from base64 import b64encode
from typing import Dict, Mapping, Union, cast
from ..configurator import Configurator, TaskView
from ..support import Status, Priority, to_kubernetes_label
from ..runtime import EntityInstance, NodeInstance, RelationshipInstance
from ..util import UnfurlTaskError, wrap_sensitive_value, sensitive_dict
from .ansible import AnsibleConfigurator
from ..yamlloader import yaml
from ..eval import Ref, RefContext, set_eval_func, map_value
from .k8s_ansible import get_api_client
import subprocess
import json


def _get_connection_config(instance):
    # Given an endpoint capability or a connection relationship, return a dictionary of connection settings.
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
        assert instance.parent
        if instance.parent is not instance.root:
            endpoint = instance.parent.attributes
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
                connection[credential["token_type"]] = credential["token"]
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
    cluster = Ref("[.type=unfurl.nodes.K8sCluster]").resolve_one(ctx)
    relation = "unfurl.relationships.ConnectsTo.K8sCluster"
    if cluster:
        instance = TaskView.find_connection(ctx, cast(NodeInstance, cluster), relation)
    else:
        relationships = cast(
            EntityInstance, ctx.currentResource
        ).get_default_relationships(relation)
        if relationships:
            instance = relationships[0]
        else:
            instance = None
    if instance:
        config = _get_connection_config(instance)
    else:
        config = {}

    # check if we are in a namespace, fall back to "default"
    namespace = Ref("[.type=unfurl.nodes.K8sNamespace]").resolve_one(ctx)
    if namespace:
        config["namespace"] = cast(NodeInstance, namespace).attributes["name"]
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
      name: { name }
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

    def can_run(self, task):
        if task.configSpec.operation not in ["check", "discover"]:
            return "Configurator can't perform this operation (only supports check and discover)"
        return True

    def can_dry_run(self, task):
        return True

    def run(self, task):
        cluster = task.target
        # the endpoint on the cluster will have connection info
        instances = cluster.get_capabilities("endpoint")
        if not instances:
            # no endpoint, look for a default connection for this cluster
            instances = cluster.get_default_relationships(
                "unfurl.relationships.ConnectsTo.K8sCluster"
            )
        if instances:
            connectionConfig = _get_connection_config(instances[0])
        else:
            connectionConfig = {}

        if cluster.attributes.get("api_server"):
            # api_server was set before, don't let it change --
            # if the connection has a different host, its pointing at a different cluster
            connectionConfig["host"] = "https://" + cluster.attributes["api_server"]

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

    def run(self, task):
        connection = task.target
        assert isinstance(connection, RelationshipInstance)
        connectionConfig = _get_connection_config(connection)
        try:
            # try connect and save the resolved host
            task.logger.debug("k8s connection configuration: %s", connectionConfig)
            connection.attributes["api_server"] = self._get_host(connectionConfig)
        except Exception:
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


class ResourceConfigurator(AnsibleConfigurator):

    @classmethod
    def set_config_spec_args(klass, kw: dict, target: EntityInstance):
        artifact = target.template.find_or_create_artifact(
            "kubernetes.core", predefined=True
        )
        if artifact:
            kw["dependencies"].append(artifact)
        return kw

    def get_generator(self, task):
        if task.dry_run:
            return self.dry_run(task)
        else:
            return self.run(task)

    def dry_run(self, task):
        task.logger.info("dry run: generating playbook:\n%s", self.find_playbook(task))
        yield task.done(True)

    def make_secret(self, data, type="Opaque"):
        return dict(
            type=type,
            apiVersion="v1",
            kind="Secret",
            data=sensitive_dict(
                {k: b64encode(str(v).encode()).decode() for k, v in data.items() if v is not None}
            ),
        )

    def get_definition(self, task):
        if task.target.template.is_compatible_type("unfurl.nodes.K8sNamespace"):
            return dict(apiVersion="v1", kind="Namespace")

        # get copy so subsequent modifications don't affect the definition
        if "definition" in task.target.attributes:
            definition = task.target.attributes.get_copy("definition") or {}
        elif "src" in task.target.attributes:
            with open(task.target.attributes["src"]) as f:
                definition = f.read()
        else:
            definition = task.target.attributes.get_copy("apiResource") or {}

        if (
            task.target.template.is_compatible_type("unfurl.nodes.K8sSecretResource")
            and "data" in task.target.attributes
        ):
            return self.make_secret(
                task.target.attributes["data"], task.target.attributes["type"]
            )
        else:
            if isinstance(definition, str):
                definition = yaml.load(definition)
            kind = definition.get("kind")
            if not kind:
                raise UnfurlTaskError(
                    task, 'invalid Kubernetes resource definition, "kind" is missing'
                )
            if kind == "Secret" and "data" in definition:
                definition["data"] = wrap_sensitive_value(definition["data"])
            return definition

    def update_metadata(self, definition, task, namespace):
        md = definition.setdefault("metadata", {})
        if namespace and "namespace" not in md:
            md["namespace"] = namespace
        # else: error if namespace mismatch?

        name = to_kubernetes_label(task.target.attributes.get("name", task.target.name))
        if "name" in md and md["name"] != name:
            task.target.attributes["name"] = md["name"]
        else:
            md["name"] = name

    def _make_check(self, connectionConfig, definition, extra_configuration):
        moduleSpec = connectionConfig.copy()
        moduleSpec["kind"] = definition.get("kind", "")
        moduleSpec["name"] = definition["metadata"]["name"]
        if "namespace" in definition["metadata"]:
            moduleSpec["namespace"] = definition["metadata"]["namespace"]
        if extra_configuration:
            moduleSpec.update(extra_configuration)
        return [{"kubernetes.core.k8s_info": moduleSpec}]

    def find_playbook(self, task):
        # this is called by AnsibleConfigurator.get_playbook()
        definition = self.get_definition(task)
        connectionConfig = _get_connection(task.inputs.context)
        self.update_metadata(definition, task, connectionConfig.get("namespace"))
        extra_configuration = task.inputs.get("configuration")
        extra_playbook = task.inputs.get("playbook")

        if task.configSpec.operation in ["check", "discover"]:
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
            # so the first time we try to create the resource check for its exists first
            task.logger.trace(
                "adding k8s resource existence check for %s", task.target.name
            )
            # don't pass the same extra_configuration to make_check
            return self._make_check(connectionConfig, definition, None) + playbook
        else:
            return playbook

    def process_result(self, task, result):
        # overrides super.processResult
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
        return result

    def get_result_keys(self, task, results):
        # save first time even if it hasn't changed
        return ["resources", "result", "duration"]  # also "method", "diff", invocation
