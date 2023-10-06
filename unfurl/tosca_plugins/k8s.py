# Generated by tosca.yaml2python from unfurl/tosca_plugins/k8s.yaml at 2023-10-06T07:19:18 overwrite not modified (change to "ok" to allow)

import unfurl
from typing import List, Dict, Any, Tuple, Union, Sequence
from typing_extensions import Annotated
from tosca import (
    ArtifactType,
    Attribute,
    B,
    BPS,
    B_Scalar,
    Bitrate,
    Capability,
    CapabilityType,
    D,
    DataType,
    Eval,
    Frequency,
    GB,
    GBPS,
    GB_Scalar,
    GHZ,
    GHz,
    GHz_Scalar,
    GIB,
    GIBPS,
    Gbps,
    Gbps_Scalar,
    GiB,
    GiB_Scalar,
    Gibps,
    Gibps_Scalar,
    GroupType,
    H,
    HZ,
    Hz,
    Hz_Scalar,
    InterfaceType,
    KB,
    KBPS,
    KHZ,
    KIB,
    KIBPS,
    Kbps,
    Kbps_Scalar,
    KiB,
    KiB_Scalar,
    Kibps,
    Kibps_Scalar,
    M,
    MB,
    MBPS,
    MB_Scalar,
    MHZ,
    MHz,
    MHz_Scalar,
    MIB,
    MIBPS,
    MS,
    Mbps,
    Mbps_Scalar,
    MiB,
    MiB_Scalar,
    Mibps,
    Mibps_Scalar,
    NS,
    Namespace,
    NodeType,
    PolicyType,
    Property,
    REQUIRED,
    MISSING,
    RelationshipType,
    Requirement,
    S,
    Size,
    T,
    TB,
    TBPS,
    TB_Scalar,
    TIB,
    TIBPS,
    Tbps,
    Tbps_Scalar,
    TiB,
    TiB_Scalar,
    Tibps,
    Tibps_Scalar,
    Time,
    ToscaDataType,
    ToscaInputs,
    ToscaOutputs,
    US,
    b,
    bps,
    bps_Scalar,
    d,
    d_Scalar,
    equal,
    field,
    gb,
    gbps,
    ghz,
    gib,
    gibps,
    greater_or_equal,
    greater_than,
    h,
    h_Scalar,
    hz,
    in_range,
    kB,
    kB_Scalar,
    kHz,
    kHz_Scalar,
    kb,
    kbps,
    khz,
    kib,
    kibps,
    length,
    less_or_equal,
    less_than,
    loader,
    m,
    m_Scalar,
    max_length,
    mb,
    mbps,
    metadata_to_yaml,
    mhz,
    mib,
    mibps,
    min_length,
    ms,
    ms_Scalar,
    ns,
    ns_Scalar,
    operation,
    pattern,
    s,
    s_Scalar,
    tb,
    tbps,
    tib,
    tibps,
    tosca_timestamp,
    tosca_version,
    us,
    us_Scalar,
    valid_values,
)
import tosca
import unfurl.configurators.k8s
from unfurl.tosca_plugins.artifacts import *


class unfurl_capabilities_Endpoint_K8sCluster(tosca.capabilities.EndpointAdmin):
    """
    Capability to connect to a K8sCluster. See unfurl.relationships.ConnectsTo.K8sCluster for the semantics of the "credential" properties.
    """

    _type_name = "unfurl.capabilities.Endpoint.K8sCluster"
    protocol: str = "https"


class unfurl_relationships_ConnectsTo_K8sCluster(tosca.relationships.ConnectsTo):
    _type_name = "unfurl.relationships.ConnectsTo.K8sCluster"
    name: Union[str, None] = Property(
        title="Cluster name",
        metadata={"env_vars": ["KUBE_CTX_CLUSTER"]},
        default=Eval({"get_env": "KUBE_CTX_CLUSTER"}),
    )
    KUBECONFIG: Union[str, None] = Property(
        metadata={"user_settable": False}, default=Eval({"get_env": "KUBECONFIG"})
    )
    """
    Path to an existing Kubernetes config file. If not provided, and no other connection options are provided, and the KUBECONFIG environment variable is not set, the default location will be used (~/.kube/config.json).
    """

    context: Union[str, None] = Property(
        metadata={"env_vars": ["KUBE_CTX"]}, default=Eval({"get_env": "KUBE_CTX"})
    )
    """
    The name of a context found in the config file. If not set the current-context will be used.
    """

    cluster_ca_certificate: Union[str, None] = Property(
        title="CA certificate",
        metadata={
            "sensitive": True,
            "user_settable": True,
            "input_type": "textarea",
            "env_vars": ["KUBE_CLUSTER_CA_CERT_DATA"],
        },
        default=Eval({"get_env": "KUBE_CLUSTER_CA_CERT_DATA"}),
    )
    """
    PEM-encoded root certificates bundle for TLS authentication
    """

    cluster_ca_certificate_file: Union[str, None] = Eval(
        {
            "eval": {
                "if": ".::cluster_ca_certificate",
                "then": {
                    "eval": {
                        "tempfile": {"eval": ".::cluster_ca_certificate"},
                        "suffix": ".crt",
                    }
                },
                "else": None,
            }
        }
    )
    insecure: Union[bool, None] = Property(
        metadata={"user_settable": True, "env_vars": ["KUBE_INSECURE"]},
        default=Eval({"get_env": "KUBE_INSECURE"}),
    )
    """
    If true, the server's certificate will not be checked for validity. This will make your HTTPS connections insecure
    """

    token: Union[str, None] = Property(
        title="Authentication Token",
        metadata={"sensitive": True, "user_settable": True, "env_vars": ["KUBE_TOKEN"]},
        default=Eval({"get_env": "KUBE_TOKEN"}),
    )
    """Token of your service account."""

    credential: Union["tosca.datatypes.Credential", None] = Property(
        metadata={"sensitive": True, "user_settable": False}, default=None
    )
    """
    token_type is either "api_key" or "password" (default is "password") Its "keys" map can have the following values: "cert_file": Path to a cert file for the certificate authority "ca_cert": Path to a client certificate file for TLS "key_file": Path to a client key file for TLS
    """

    namespace: Union[str, None] = None
    """The default namespace scope to use"""

    api_server: Union[str, None] = Property(
        title="Kubernetes Cluster API Base URL",
        metadata={"user_settable": True, "env_vars": ["KUBE_HOST"]},
        default=Eval({"eval": ".target::api_server"}),
    )
    """The address and port of the Kubernetes API server"""

    protocol: str = "https"
    as_: Union[str, None] = Property(
        name="as", metadata={"user_settable": False}, default=None
    )
    """Username to impersonate for the operation"""

    as_group: Union[List[str], None] = Property(
        name="as-group", metadata={"user_settable": False}, default=None
    )
    """Groups to impersonate for the operation"""

    def check(self, **kw):
        return unfurl.configurators.k8s.ConnectionConfigurator()

    _valid_target_types = ["unfurl_capabilities_Endpoint_K8sCluster"]


class unfurl_nodes_K8sCluster(tosca.nodes.Root):
    _type_name = "unfurl.nodes.K8sCluster"
    api_server: str = Attribute(metadata={"immutable": True})
    """The address and port of the cluster's API server"""

    host: "tosca.capabilities.Container" = Capability(
        factory=tosca.capabilities.Container,
        valid_source_types=["unfurl.nodes.K8sRawResource", "unfurl.nodes.K8sNamespace"],
    )

    endpoint: "unfurl_capabilities_Endpoint_K8sCluster" = Capability(
        factory=unfurl_capabilities_Endpoint_K8sCluster
    )

    def check(self, **kw):
        return unfurl.configurators.k8s.ClusterConfigurator()

    def discover(self, **kw):
        return unfurl.configurators.k8s.ClusterConfigurator()


class unfurl_nodes_K8sRawResource(tosca.nodes.Root):
    _type_name = "unfurl.nodes.K8sRawResource"
    definition: Union[Any, None] = None
    """Inline resource definition (string or map)"""

    src: Union[str, None] = Property(metadata={"user_settable": False}, default=None)
    """File path to resource definition"""

    apiResource: Union[Dict[str, Any], None] = Attribute()
    name: str = Attribute()

    host: Union[
        Union["tosca.relationships.HostedOn", "unfurl_nodes_K8sCluster"], None
    ] = None

    def check(self, **kw):
        return unfurl.configurators.k8s.ResourceConfigurator()

    def discover(self, **kw):
        return unfurl.configurators.k8s.ResourceConfigurator()

    def configure(self, **kw):
        return unfurl.configurators.k8s.ResourceConfigurator()

    def delete(self, **kw):
        return unfurl.configurators.k8s.ResourceConfigurator()


class unfurl_nodes_K8sNamespace(unfurl_nodes_K8sRawResource):
    _type_name = "unfurl.nodes.K8sNamespace"
    name: Annotated[str, (pattern("^[\\w-]+$"),)] = Property(
        metadata={"immutable": True}, default="default"
    )

    host_capability: "tosca.capabilities.Container" = Capability(
        name="host",
        factory=tosca.capabilities.Container,
        valid_source_types=["unfurl.nodes.K8sResource"],
    )


class unfurl_nodes_K8sResource(unfurl_nodes_K8sRawResource):
    _type_name = "unfurl.nodes.K8sResource"
    name: str = Eval({"eval": ".name"})

    namespace: str = Attribute()

    host: Union[
        Union["tosca.relationships.HostedOn", "unfurl_nodes_K8sNamespace"], None
    ] = None


class unfurl_nodes_K8sSecretResource(unfurl_nodes_K8sResource):
    _type_name = "unfurl.nodes.K8sSecretResource"
    definition: Union[Any, None] = Property(
        metadata={"user_settable": False}, default=None
    )
    """Inline resource definition (string or map)"""

    type: str = "Opaque"
    data: Union[Dict[str, Any], None] = Property(
        metadata={"sensitive": True}, default=None
    )


kube_artifacts = unfurl.nodes.LocalRepository(
    "kube-artifacts",
    _directives=["default"],
)
kube_artifacts.kubectl = artifact_AsdfTool("kubectl", version="1.25.3", file="kubectl")


if __name__ == "__main__":
    tosca.dump_yaml(globals())

