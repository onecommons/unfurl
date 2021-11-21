# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
from __future__ import absolute_import
import codecs
import re
from ..configurator import Configurator, Status
from ..runtime import RelationshipInstance
from .ansible import AnsibleConfigurator
import json
from ansible_collections.kubernetes.core.plugins.module_utils.common import (
    K8sAnsibleMixin,
)


def _get_connection_config(instance, cluster):
    # see https://docs.ansible.com/ansible/latest/modules/k8s_module.html#k8s-module
    #  for connection settings
    if not instance:
        return {}
    connect = {}
    if isinstance(instance, RelationshipInstance):
        connect = instance.attributes
        # note: endpoint will be root if instance is a default connection
        if instance.parent is not instance.root:
            endpoint = instance.parent.attributes
        else:
            endpoint = {}
    else:
        endpoint = instance.attributes

    connection = {}
    # cluster only:
    if cluster.attributes.get("api_server"):
        connection["host"] = "https://" + cluster.attributes["api_server"]

    # endpoint capability only
    protocol = endpoint.get("protocol")
    ip_address = endpoint.get("ip_address")
    port = endpoint.get("port")
    if ip_address and port:
        connection["host"] = f"{protocol}://{ip_address}:{port}"

    # connection only:
    api_server = connect.get("api_server")
    if api_server:
        connection["host"] = f"https://{api_server}"

    map1 = {
        "KUBECONFIG": "kubeconfig",
        "context": "context",
        "secure": "verify_ssl",
        "namespace": "namespace",
    }
    # relationship overrides capability
    for attributes in [endpoint, connect]:
        for key, value in map1.items():
            if key in attributes:
                connection[value] = attributes[key]

        credential = attributes.get("credential")
        if credential:
            if credential.get("token_type") in ["api_key", "password"]:
                connection[credential["token_type"]] = credential["token"]
            if "user" in credential:
                connection["username"] = credential["user"]
            if "keys" in credential:
                # ["ssl_ca_cert", "cert_file", "key_file"]
                connection.update(credential["keys"])

    return connection


def _get_connection(task, cluster):
    instance = task.find_connection(
        cluster, relation="unfurl.relationships.ConnectsTo.K8sCluster"
    )
    return _get_connection_config(instance, cluster)


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
        connectionConfig = _get_connection(task, cluster)
        try:
            cluster.attributes["api_server"] = self._get_host(connectionConfig)
        except:
            yield task.done(
                False,
                captureException="error while trying to establish connection to cluster",
            )
        else:
            # we aren't modifying this cluster but we do want to assert that its ok
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
        # print(self.findPlaybook(task))
        # print(self.findPlaybook(task))
        print(json.dumps(self.find_playbook(task), indent=4))
        yield task.done(True)

    def _get_connection(self, task):
        # get the cluster that the target resource is hosted on
        cluster = task.query("[.type=unfurl.nodes.K8sCluster]")
        if not cluster:
            return {}
        return _get_connection(task, cluster)

    def make_secret(self, data):
        # base64 adds trailing \n so strip it out
        return dict(
            type="Opaque",
            apiVersion="v1",
            kind="Secret",
            data={
                k: codecs.encode(str(v).encode(), "base64").decode().strip()
                for k, v in data.items()
            },
        )

    def get_definition(self, task):
        if task.target.template.is_compatible_type("unfurl.nodes.K8sNamespace"):
            return dict(apiVersion="v1", kind="Namespace")

        if "definition" in task.target.attributes:
            definition = task.target.attributes.get_copy("definition") or {}
        else:
            definition = task.target.attributes.get_copy("apiResource") or {}

        if not definition and task.target.template.is_compatible_type(
            "unfurl.nodes.K8sSecretResource"
        ):
            return self.make_secret(task.target.attributes.get("data", {}))
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
        connectionConfig = self._get_connection(task)
        moduleSpec = dict(state=state, **connectionConfig)
        if task.configSpec.operation in ["check", "discover"]:
            moduleSpec["kind"] = definition.get("kind", "")
            moduleSpec["name"] = definition["metadata"]["name"]
            if "namespace" in definition["metadata"]:
                moduleSpec["namespace"] = definition["metadata"]["namespace"]
        else:
            moduleSpec["resource_definition"] = definition
        return [{"community.kubernetes.k8s": moduleSpec}]

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
                status = resource.get("status", {}).get("phase", "Unknown")
                result.status = states.get(status, Status.unknown)
        return result

    def get_result_keys(self, task, results):
        # save first time even if it hasn't changed
        return ["result"]  # also "method", "diff", invocation
