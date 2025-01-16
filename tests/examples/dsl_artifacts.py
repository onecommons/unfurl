from abc import abstractmethod
from typing import Dict
import unfurl
import tosca
from tosca import ToscaInputs, ToscaOutputs, Attribute, placeholder

class KubernetesClusterInputs(ToscaInputs):
    do_region: str = "nyc3"
    doks_k8s_version: str ="1.30"

class KubernetesClusterOutputs(ToscaOutputs):
    do_id: str = Attribute()  # needed so can be KubernetesClusterOutputs inherited 

class ClusterOp(unfurl.artifacts.Executable):
    file: str = "kubernetes"

    # inputs defined here are set as the inputs for operations that set this artifact as their implementation
    # args, retval set input, output definitions
    def execute(self, inputs: KubernetesClusterInputs) -> KubernetesClusterOutputs:
        # If an artifact of this type is use an operation's implementation
        # the inputs defined here will be set as the inputs definition for the operation
        return placeholder(KubernetesClusterOutputs)

class MoreInputs(ToscaInputs):
    nodes: int = 4


class CustomClusterOp(ClusterOp):
    # customizing an artifact should make sure execute is compatible with base artifact's artifact
    # this means you can't change existing parameters, only add new ones with default values

    # only add new inputs definitions to this types interface, since the base types inputs will be merged with these
    def execute(self, inputs: KubernetesClusterInputs, more_inputs: MoreInputs = MoreInputs()) -> KubernetesClusterOutputs:
        self.set_inputs(inputs, more_inputs)
        return placeholder(KubernetesClusterOutputs)

class ClusterTerraform(unfurl.artifacts.TerraformModule, ClusterOp):
    # need to merge properties with inputs for configurator
    file: str = "kubernetes"

    # def execute(self, inputs: KubernetesClusterInputs) -> KubernetesClusterOutputs:
    #     ToscaInputs._get_inputs(self)  # type: ignore
    #     return None # type: ignore
    #     # print("Cluster.execute!!", inputs)
    #     # return configurator(self.className)(self).execute(inputs)

class DOCluster(tosca.nodes.Root, KubernetesClusterInputs, KubernetesClusterOutputs):
    clusterconfig: "ClusterOp" = ClusterTerraform()

    my_property: str = "default"

    def configure(self, **kw) -> KubernetesClusterOutputs:
        return self.clusterconfig.execute(self)


class ExtraClusterOp(ClusterOp):
    # properties are merged with configurator inputs at runtime
    extra: str = tosca.Property(options=tosca.InputOption)

    def execute(self, inputs: KubernetesClusterInputs, extra: str = tosca.CONSTRAINED) -> KubernetesClusterOutputs:
        # self.set_inputs(inputs, extra=self.extra)
        return KubernetesClusterOutputs()

class CustomClusterTerraform(unfurl.artifacts.TerraformModule, ExtraClusterOp):
    file: str = "my_custom_kubernetes_tf_module"

mycluster = DOCluster(clusterconfig=CustomClusterTerraform(extra="extra", 
                              contents = """resource "null_resource" "null" {}
                                  output "do_id" {
                                    value = "ABC"
                                  }
 """))



