import tosca
from tosca_repositories.std.aws.compute import EC2Compute
from tosca_repositories.std.aws.db import AwsRdsPostgres
from tosca_repositories.std import k8s
from tosca_repositories.std.dns_services import Route53DNSZone

class production(tosca.DeploymentBlueprint):
    _cloud = unfurl.relationships.ConnectsToAWSAccount

    host = std.HttpsProxyContainerComputeHost(
        host=EC2Compute(
            disk_size=Inputs.disk_size,
            num_cpus=2,
            mem_size=Inputs.mem_size,
        ),
        dns=Route53DNSZone(name="example.com"),
    )
    db = AwsRdsPostgres()

class dev(tosca.DeploymentBlueprint):
    # unfurl_relationships_ConnectsTo_K8sCluster
    _cloud = "unfurl.relationships.ConnectsTo.K8sCluster"

    host = k8s.PublicK8sContainerHost(
        labels={"kompose.volume.size": Inputs.disk_size}
    )
    db = std.PostgresDBInstance(
        database_name="my_db", host_requirement=k8s.PrivateK8sContainerHost()
    )
