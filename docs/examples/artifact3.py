import unfurl
import tosca

class ClusterOp(unfurl.artifacts.Executable):
    file: str = "kubernetes"

    def execute(self, prop1: str, prop2: int):
        pass

class DOCluster(tosca.nodes.Root):
    cluster_config: "ClusterOp"

    my_property: str = "default"

    def configure(self, **kw):
        return self.cluster_config.execute(prop1=self.my_property, prop2=0)

class ClusterTerraform(unfurl.artifacts.TerraformModule, ClusterOp):
    file: str = "main.tf"

mycluster = DOCluster(cluster_config=ClusterTerraform())
