# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
import codecs
import re
from base64 import b64encode
from ..configurator import Configurator
from ..support import Status, Priority
from ..runtime import RelationshipInstance
from ..util import save_to_tempfile
from .ansible import AnsibleConfigurator
import json
from ansible_collections.kubernetes.core.plugins.module_utils.common import (
    K8sAnsibleMixin,
)


def _get_connection_config(instance):
    # Given an endpoint capability or a connection relationship, return a dictionary of connection settings.
    # https://docs.ansible.com/ansible/latest/collections/kubernetes/core/k8s_module.html
    #  for connection settings
    # https://github.com/ansible-collections/kubernetes.core/blob/7f7008fecc9e5d16340e9b0bff510b7cde2f2cfd/plugins/connection/kubectl.py
    if not instance:
        return {}
    connection = {}
    if isinstance(instance, RelationshipInstance):
        connect = instance.attributes
        # the parent of a relationship will be the capability it connects to
        # or root if relationship is a default connection
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
        if '//' in api_server:
            connection["host"] = api_server
        else:
            connection["host"] = f"https://{api_server}"

    map1 = {
        "KUBECONFIG": "kubeconfig",
        "context": "context",
        "namespace": "namespace",
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
            if "keys" in credential:
                # ["ca_cert", "cert_file", "key_file"]
                connection.update(credential["keys"])

        if attributes.get("token"):
            connection["api_key"] = attributes["token"]

        if "ca_cert" not in connection and attributes.get(
            "cluster_ca_certificate"
        ):
            connection["ca_cert"] = save_to_tempfile(
                attributes["cluster_ca_certificate"], ".crt"
            ).name
    return connection


CONNECTION_OPTIONS = {
    "namespace": "-n",
    "kubeconfig": "--kubeconfig",
    "context": "--context",
    "kubectl_host": "--server",
    "username": "--username",
    "password": "--password",
    "client_cert": "--client-certificate",
    "client_key": "--client-key",
    "ca_cert": "--certificate-authority",
    "api_key": "--token",
}


def _get_connection(task) -> dict:
    # get the cluster that the target resource is hosted on
    cluster = task.query("[.type=unfurl.nodes.K8sCluster]")
    relation = "unfurl.relationships.ConnectsTo.K8sCluster"
    if cluster:
        instance = task.find_connection(cluster, relation)
    else:
        relationships = task.target.get_default_relationships(relation)
        if relationships:
            instance = relationships[0]
        else:
            instance = None
    if instance:
        config = _get_connection_config(instance)
    else:
        config = {}

    # check if we are in a namespace
    namespace = task.query("[.type=unfurl.nodes.K8sNamespace]")
    if namespace:
        config["namespace"] = namespace.attributes["name"]
    return config


def get_kubectl_args(task):
    connection = _get_connection(task)
    options = []
    for key, value in connection.items():
        if value and key in CONNECTION_OPTIONS:
            options.append(CONNECTION_OPTIONS[key])
            options.append(value)
    if "validate_certs" in connection and not connection["validate_certs"]:
        options.append("--insecure-skip-tls-verify")
    return options


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
        client = K8sAnsibleMixin().get_api_client(**connectionConfig)
        url = client.configuration.host
        if url:
            return re.sub("^https?://", "", url)
        return url

    def can_run(self, task):
        if task.configSpec.operation not in ["check", "discover"]:
            return "Configurator can't perform this operation (only supports check and discover)"
        return True

    def run(self, task):
        cluster = task.target
        # the endpoint on the cluster will have connection info
        instances = cluster.get_capabilities("endpoint")
        if not instances:
            # no endpoint, look for a default connection for this cluster
            instances = cluster.get_default_relationships("unfurl.relationships.ConnectsTo.K8sCluster")
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
                False, False,
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
            task.logger.debug(connectionConfig)
            connection.attributes["api_server"] = self._get_host(connectionConfig)
        except Exception:
            yield task.done(
                False, False,
                captureException="error while trying to establish connection to cluster",
            )
        else:
            # we aren't modifying this connection but we do want to assert that its ok
            yield task.done(True, False, Status.ok)


class ResourceConfigurator(AnsibleConfigurator):
    def get_generator(self, task):
        if task.dry_run:
            return self.dry_run(task)
        else:
            return self.run(task)

    def dry_run(self, task):
        # XXX don't use print()
        print("generating playbook")
        print(json.dumps(self.find_playbook(task), indent=4))
        yield task.done(True)

    def make_secret(self, data):
        return dict(
            type="Opaque",
            apiVersion="v1",
            kind="Secret",
            data={k: b64encode(str(v).encode()).decode() for k, v in data.items()},
        )

    def get_definition(self, task):
        if task.target.template.is_compatible_type("unfurl.nodes.K8sNamespace"):
            return dict(apiVersion="v1", kind="Namespace")

        if "definition" in task.target.attributes:
            definition = task.target.attributes.get_copy("definition") or {}
        else:
            definition = task.target.attributes.get_copy("apiResource") or {}

        if (
            task.target.template.is_compatible_type("unfurl.nodes.K8sSecretResource")
            and "data" in task.target.attributes
        ):
            return self.make_secret(task.target.attributes["data"])
        else:
            # XXX if definition is string: parse
            # get copy so subsequent modifications dont affect the definition
            return definition

    def update_metadata(self, definition, task):
        namespace = None
        if task.target.parent.template.is_compatible_type("unfurl.nodes.K8sNamespace"):
            namespace = task.target.parent.attributes["name"]
        md = definition.setdefault("metadata", {})
        if namespace and "namespace" not in md:
            md["namespace"] = namespace
        # else: error if namespace mismatch?

        # XXX if using target.name, convert into kube friendly dns-style name
        name = task.target.attributes.get("name", task.target.name)
        if "name" in md and md["name"] != name:
            task.target.attributes["name"] = md["name"]
        else:
            md["name"] = name

    def find_playbook(self, task):
        definition = self.get_definition(task)
        self.update_metadata(definition, task)
        delete = task.configSpec.operation in ["Standard.delete", "delete"]
        state = "absent" if delete else "present"
        connectionConfig = _get_connection(task)
        moduleSpec = dict(state=state, **connectionConfig)
        if task.target.attributes.get("src"):
            # XXX set FilePath
            moduleSpec["src"] = task.target.attributes["src"]
        elif task.configSpec.operation in ["check", "discover"]:
            moduleSpec["kind"] = definition.get("kind", "")
            moduleSpec["name"] = definition["metadata"]["name"]
            if "namespace" in definition["metadata"]:
                moduleSpec["namespace"] = definition["metadata"]["namespace"]
        else:
            moduleSpec["resource_definition"] = definition
        return [{"kubernetes.core.k8s": moduleSpec}]

    def process_result(self, task, result):
        # overrides super.processResult
        resource = result.result.get("result")
        task.target.attributes["apiResource"] = resource
        if resource:
            data = resource.get("kind") == "Secret" and resource.get("data")
            if data:
                resource["data"] = {k: task.sensitive(v) for k, v in data.items()}
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
        return ["result"]  # also "method", "diff", invocation
