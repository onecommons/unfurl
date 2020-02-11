from __future__ import absolute_import
import codecs
from ..util import sensitive_str
from ..configurator import Configurator, Status
from ..runtime import RelationshipInstance
from .ansible import AnsibleConfigurator
import json
from ansible.module_utils.k8s.common import K8sAnsibleMixin


def _getConnectionConfig(connectionInstance):
    # XXX
    # both connection and it's parent (the endpoint) might have "credential" property set
    # credentials = connection.query("credential", wantList=True)
    # connection.attributes: context, KUBECONFIG, secure
    return dict(context=connectionInstance.attributes.get("context"))


class ClusterConfigurator(Configurator):
    @staticmethod
    def _getHost(connectionConfig):
        client = K8sAnsibleMixin().get_api_client(**connectionConfig)
        return client.configuration.host

    def shouldRun(self, task):
        # only run this directly on a cluster if the topology doesn't define any to the cluster
        if not isinstance(task.target, RelationshipInstance):
            for endpoint in task.target.getCapabilities("endpoint"):
                if endpoint.relationships:
                    print("shouldRun", "endpoint.relationships", endpoint.relationships)
                    return False
        return True

    def canRun(self, task):
        if task.configSpec.operation not in ["check", "discover"]:
            return "Configurator can't perform this operation (only supports check and discover)"
        if not isinstance(
            task.target, RelationshipInstance
        ) and not task.target.getCapabilities("endpoint"):
            return "No endpoint defined on this cluster"
        return True

    def run(self, task):
        # this just tests the connection
        if isinstance(task.target, RelationshipInstance):
            cluster = task.target.target
            connection = task.target
        else:
            cluster = task.target
            connection = cluster.getCapabilities("endpoint")[0]

        connectionConfig = _getConnectionConfig(connection)
        try:
            cluster.attributes["apiServer"] = self._getHost(connectionConfig)
        except:
            yield task.done(
                False,
                captureException="error while trying to establish connection to cluster",
            )
        else:
            # we aren't modifying this cluster but we do want to assert that its ok
            yield task.done(True, False, Status.ok)


class ResourceConfigurator(AnsibleConfigurator):
    def getGenerator(self, task):
        if task.dryRun:
            return self.dryRun(task)
        else:
            return self.run(task)

    def dryRun(self, task):
        # XXX don't use print()
        print("generating playbook")
        # print(self.findPlaybook(task))
        # print(self.findPlaybook(task))
        print(json.dumps(self.findPlaybook(task), indent=4))
        yield task.done(True)

    def _getConnection(self, task):
        # get the cluster that the target resource is hosted on
        cluster = task.query("[.type=unfurl.nodes.K8sCluster]")
        if not cluster:
            return {}
        # find the operation_host's connection to that cluster
        connection = task.query(
            "$OPERATION_HOST::.requirements::*[.type=unfurl.relationships.ConnectsTo.K8sCluster][.target=$cluster]",
            vars=dict(cluster=cluster),
        )
        # alternative query: [.type=unfurl.nodes.K8sCluster]::.capabilities::.relationships::[.type=unfurl.relationships.ConnectsTo.K8sCluster][.source=$OPERATION_HOST]
        if not connection:
            # no connection, use the defaults provided by the cluster's endpoint
            endpoints = cluster.getCapabilities("endpoint")
            if endpoints:
                connection = endpoints[0]
            else:
                return {}
        return _getConnectionConfig(connection)

    def makeSecret(self, data):
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

    def getDefinition(self, task):
        if task.target.template.isCompatibleType("unfurl.nodes.K8sNamespace"):
            return dict(apiVersion="v1", kind="Namespace")
        elif task.target.template.isCompatibleType("unfurl.nodes.K8sSecretResource"):
            return self.makeSecret(task.target.attributes.get("data", {}))
        else:
            # XXX if definition is string: parse
            # get copy so subsequent modifications dont affect the definition
            return task.target.attributes.getCopy("definition", {})

    def updateMetadata(self, definition, task):
        namespace = None
        if task.target.parent.template.isCompatibleType("unfurl.nodes.K8sNamespace"):
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

    def findPlaybook(self, task):
        definition = self.getDefinition(task)
        self.updateMetadata(definition, task)
        delete = task.configSpec.operation == "Standard.delete"
        state = "absent" if delete else "present"
        connectionConfig = self._getConnection(task)
        moduleSpec = dict(state=state, definition=definition, **connectionConfig)
        return [dict(k8s=moduleSpec)]

    def _processResult(self, task, result):
        resource = result.get("result")
        task.target.attributes["apiResource"] = resource
        data = resource and resource.get("kind") == "Secret" and resource.get("data")
        if data:
            resource["data"] = {k: sensitive_str(v) for k, v in data.items()}

    def getResultKeys(self, task, results):
        # save first time even if it hasn't changed
        return ["result"]  # also "method", "diff", invocation
