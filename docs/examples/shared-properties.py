import unfurl
import tosca

class KubernetesClusterInputs(tosca.ToscaInputs):
    do_region: str = "nyc3" 

class KubernetesClusterOutputs(tosca.ToscaOutputs):
    do_id: str = tosca.Attribute()

class ClusterTerraformModule(unfurl.artifacts.TerraformModule):
    file: str = "my_module/main.tf"
    def execute(self, inputs: KubernetesClusterInputs) -> KubernetesClusterOutputs:
        return KubernetesClusterOutputs()

# inherited ToscaInputs and ToscaOutputs are treated as properties and attributes
class Cluster(tosca.nodes.Root, KubernetesClusterInputs, KubernetesClusterOutputs):
    cluster_config: "ClusterTerraformModule" = ClusterTerraformModule()

    my_property: str = "default"

    def configure(self) -> KubernetesClusterOutputs:
        return self.cluster_config.execute(self)

