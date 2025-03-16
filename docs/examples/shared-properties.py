import unfurl
import tosca

class KubernetesClusterInputs(tosca.ToscaInputs):
    do_region: str = "nyc3" 

class KubernetesClusterOutputs(tosca.ToscaOutputs):
    do_id: str = tosca.Attribute()

class ClusterOp(unfurl.artifacts.Executable):
    def execute(self, inputs: KubernetesClusterInputs) -> KubernetesClusterOutputs:
        return KubernetesClusterOutputs()

class Cluster(tosca.nodes.Root, KubernetesClusterInputs, KubernetesClusterOutputs):
    clusterconfig: "ClusterOp" = ClusterOp(file="")

    my_property: str = "default"

    def configure(self):
        # return the implementation artifact... if you don't call execute() it defaults to properties inherited from KubernetesClusterInputs
        return self.clusterconfig
