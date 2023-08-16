# Generated by tosca.yaml2python from ../unfurl/vendor/toscaparser/elements/TOSCA_definition_1_3.yaml at 2023-08-16 12:55:01.404809

from typing import List, Dict, Any, Tuple, Union
import unfurl
from tosca import (
    Size,
    Time,
    Frequency,
    Bitrate,
    Namespace,
    tosca_version,
    tosca_timestamp,
    Property,
    Attribute,
    Requirement,
    Capability,
    InterfaceType,
    NodeType,
    RelationshipType,
    DataType,
)
from tosca import *
import tosca


class interfaces(Namespace):
    class Root(InterfaceType):
        """
        The TOSCA root Interface Type all other TOSCA Interface types derive from
        """

        _tosca_name = "tosca.interfaces.Root"

    class NodeLifecycleStandard(InterfaceType):
        _tosca_name = "tosca.interfaces.node.lifecycle.Standard"

        def create(self):
            """Standard lifecycle create operation."""

        def configure(self):
            """Standard lifecycle configure operation."""

        def start(self):
            """Standard lifecycle start operation."""

        def stop(self):
            """Standard lifecycle stop operation."""

        def delete(self):
            """Standard lifecycle delete operation."""

    class RelationshipConfigure(InterfaceType):
        _tosca_name = "tosca.interfaces.relationship.Configure"

        def pre_configure_source(self):
            """Operation to pre-configure the source endpoint."""

        def pre_configure_target(self):
            """Operation to pre-configure the target endpoint."""

        def post_configure_source(self):
            """Operation to post-configure the source endpoint."""

        def post_configure_target(self):
            """Operation to post-configure the target endpoint."""

        def add_target(self):
            """Operation to add a target node."""

        def remove_target(self):
            """Operation to remove a target node."""

        def add_source(self):
            """
            Operation to notify the target node of a source node which is now available via a relationship.
            """

        def remove_source(self):
            """
            Operation to notify the target node of a source node which is no longer available via a relationship.
            """

        def target_changed(self):
            """
            Operation to notify source some property or attribute of the target changed
            """


class datatypes(Namespace):
    class Root(DataType):
        """
        The TOSCA root Data Type all other TOSCA base Data Types derive from
        """

        _tosca_name = "tosca.datatypes.Root"

    class NetworkNetworkInfo(Root):
        _tosca_name = "tosca.datatypes.network.NetworkInfo"
        network_name: str
        network_id: str
        addresses: List[str]

    class NetworkPortInfo(Root):
        _tosca_name = "tosca.datatypes.network.PortInfo"
        port_name: str
        port_id: str
        network_id: str
        mac_address: str
        addresses: List[str]

    class NetworkPortDef(Root):
        _tosca_name = "tosca.datatypes.network.PortDef"
        _type = "integer"
        _constraints = [{"in_range": [1, 65535]}]

    class NetworkPortSpec(Root):
        _tosca_name = "tosca.datatypes.network.PortSpec"
        protocol: Annotated[str, (valid_values(["udp", "tcp", "icmp"]),)] = "tcp"
        target: Union[int, None] = None
        target_range: Union[
            Annotated[Tuple[int, int], (in_range([1, 65535]),)], None
        ] = None
        source: Union[int, None] = None
        source_range: Union[
            Annotated[Tuple[int, int], (in_range([1, 65535]),)], None
        ] = None

    class Credential(Root):
        _tosca_name = "tosca.datatypes.Credential"
        protocol: Union[str, None] = None
        token_type: str = "password"
        token: str
        keys: Union[Dict[str, str], None] = None
        user: Union[str, None] = None

    class Json(Root):
        _tosca_name = "tosca.datatypes.json"
        _type = "string"

    class Xml(Root):
        _tosca_name = "tosca.datatypes.xml"
        _type = "string"


class artifacts(Namespace):
    class Root(ArtifactType):
        """
        The TOSCA Artifact Type all other TOSCA Artifact Types derive from
        """

        _tosca_name = "tosca.artifacts.Root"

    class File(Root):
        _tosca_name = "tosca.artifacts.File"

    class Deployment(Root):
        """TOSCA base type for deployment artifacts"""

        _tosca_name = "tosca.artifacts.Deployment"

    class DeploymentImage(Deployment):
        _tosca_name = "tosca.artifacts.Deployment.Image"

    class DeploymentImageVM(DeploymentImage):
        """Virtual Machine (VM) Image"""

        _tosca_name = "tosca.artifacts.Deployment.Image.VM"

    class Implementation(Root):
        """TOSCA base type for implementation artifacts"""

        _tosca_name = "tosca.artifacts.Implementation"

    class ImplementationBash(Implementation):
        """Script artifact for the Unix Bash shell"""

        _tosca_name = "tosca.artifacts.Implementation.Bash"
        _file_ext = ["sh"]
        _mime_type = "application/x-sh"

    class ImplementationPython(Implementation):
        """Artifact for the interpreted Python language"""

        _tosca_name = "tosca.artifacts.Implementation.Python"
        _file_ext = ["py"]
        _mime_type = "application/x-python"

    class DeploymentImageContainerDocker(DeploymentImage):
        """Docker container image"""

        _tosca_name = "tosca.artifacts.Deployment.Image.Container.Docker"

    class DeploymentImageVMISO(DeploymentImage):
        """Virtual Machine (VM) image in ISO disk format"""

        _tosca_name = "tosca.artifacts.Deployment.Image.VM.ISO"
        _file_ext = ["iso"]
        _mime_type = "application/octet-stream"

    class DeploymentImageVMQCOW2(DeploymentImage):
        """Virtual Machine (VM) image in QCOW v2 standard disk format"""

        _tosca_name = "tosca.artifacts.Deployment.Image.VM.QCOW2"
        _file_ext = ["qcow2"]
        _mime_type = "application/octet-stream"

    class Template(Root):
        """TOSCA base type for template type artifacts"""

        _tosca_name = "tosca.artifacts.template"


class capabilities(Namespace):
    class Root(CapabilityType):
        """
        The TOSCA root Capability Type all other TOSCA base Capability Types derive from.
        """

        _tosca_name = "tosca.capabilities.Root"

    class Node(Root):
        _tosca_name = "tosca.capabilities.Node"

    class Container(Root):
        _tosca_name = "tosca.capabilities.Container"

    class Storage(Root):
        _tosca_name = "tosca.capabilities.Storage"
        name: Union[str, None] = None

    class Compute(Container):
        _tosca_name = "tosca.capabilities.Compute"
        name: Union[str, None] = None
        num_cpus: Union[Annotated[int, (greater_or_equal(1),)], None] = None
        cpu_frequency: Union[
            Annotated[Frequency, (greater_or_equal(0.1),)], None
        ] = None
        disk_size: Union[Annotated[Size, (greater_or_equal(0),)], None] = None
        mem_size: Union[Annotated[Size, (greater_or_equal(0),)], None] = None

    class Endpoint(Root):
        _tosca_name = "tosca.capabilities.Endpoint"
        protocol: str = "tcp"
        port: Union[int, None] = None
        secure: Union[bool, None] = False
        url_path: Union[str, None] = None
        port_name: Union[str, None] = None
        network_name: Union[str, None] = "PRIVATE"
        initiator: Union[
            Annotated[str, (valid_values(["source", "target", "peer"]),)], None
        ] = "source"
        ports: Union[Annotated[Dict[str, dict], (min_length(1),)], None] = None

        ip_address: str = Attribute()

    class EndpointAdmin(Endpoint):
        _tosca_name = "tosca.capabilities.Endpoint.Admin"
        secure: Union[Annotated[bool, (equal(True),)], None] = True

    class EndpointPublic(Endpoint):
        _tosca_name = "tosca.capabilities.Endpoint.Public"
        network_name: Union[Annotated[str, (equal("PUBLIC"),)], None] = "PUBLIC"
        floating: bool = Property(status="experimental", default=False)
        """
       Indicates that the public address should be allocated from a pool of floating IPs that are associated with the network.
       """
        dns_name: Union[str, None] = Property(status="experimental", default=None)
        """The optional name to register with DNS"""

    class Scalable(Root):
        _tosca_name = "tosca.capabilities.Scalable"
        min_instances: int = 1
        """
       This property is used to indicate the minimum number of instances that should be created for the associated TOSCA Node Template by a TOSCA orchestrator.
       """
        max_instances: int = 1
        """
       This property is used to indicate the maximum number of instances that should be created for the associated TOSCA Node Template by a TOSCA orchestrator.
       """
        default_instances: Union[int, None] = None
        """
       An optional property that indicates the requested default number of instances that should be the starting number of instances a TOSCA orchestrator should attempt to allocate. The value for this property MUST be in the range between the values set for min_instances and max_instances properties.
       """

    class EndpointDatabase(Endpoint):
        _tosca_name = "tosca.capabilities.Endpoint.Database"

    class Attachment(Root):
        _tosca_name = "tosca.capabilities.Attachment"

    class NetworkLinkable(Root):
        """
        A node type that includes the Linkable capability indicates that it can be pointed by tosca.relationships.network.LinksTo relationship type, which represents an association relationship between Port and Network node types.
        """

        _tosca_name = "tosca.capabilities.network.Linkable"

    class NetworkBindable(Root):
        """
        A node type that includes the Bindable capability indicates that it can be pointed by tosca.relationships.network.BindsTo relationship type, which represents a network association relationship between Port and Compute node types.
        """

        _tosca_name = "tosca.capabilities.network.Bindable"

    class OperatingSystem(Root):
        _tosca_name = "tosca.capabilities.OperatingSystem"
        architecture: Union[str, None] = None
        """
       The host Operating System (OS) architecture.
       """
        type: Union[str, None] = None
        """
       The host Operating System (OS) type.
       """
        distribution: Union[str, None] = None
        """
       The host Operating System (OS) distribution. Examples of valid values for an “type” of “Linux” would include: debian, fedora, rhel and ubuntu.
       """
        version: Union[tosca_version, None] = None
        """
       The host Operating System version.
       """

    class ContainerDocker(Container):
        _tosca_name = "tosca.capabilities.Container.Docker"
        version: Union[List[tosca_version], None] = None
        """
       The Docker version capability.
       """
        publish_all: Union[bool, None] = False
        """
       Indicates that all ports (ranges) listed in the dockerfile using the EXPOSE keyword be published.
       """
        publish_ports: Union[List[dict], None] = None
        """
       List of ports mappings from source (Docker container) to target (host) ports to publish.
       """
        expose_ports: Union[List[dict], None] = None
        """
       List of ports mappings from source (Docker container) to expose to other Docker containers (not accessible outside host).
       """
        volumes: Union[List[str], None] = None
        """
       The dockerfile VOLUME command which is used to enable access from the Docker container to a directory on the host machine.
       """


class relationships(Namespace):
    class Root(RelationshipType, interfaces.RelationshipConfigure):
        """
        The TOSCA root Relationship Type all other TOSCA base Relationship Types derive from.
        """

        _tosca_name = "tosca.relationships.Root"
        tosca_id: str = Attribute()
        tosca_name: str = Attribute()

    class DependsOn(Root):
        _tosca_name = "tosca.relationships.DependsOn"

    class HostedOn(Root):
        _tosca_name = "tosca.relationships.HostedOn"

    class ConnectsTo(Root):
        _tosca_name = "tosca.relationships.ConnectsTo"
        credential: Union["datatypes.Credential", None] = None

        _valid_target_types = ["capabilities.Endpoint"]

    class AttachesTo(Root):
        _tosca_name = "tosca.relationships.AttachesTo"
        location: Annotated[str, (min_length(1),)]
        device: Union[str, None] = None

        _valid_target_types = ["capabilities.Attachment"]

    class RoutesTo(ConnectsTo):
        _tosca_name = "tosca.relationships.RoutesTo"
        _valid_target_types = ["capabilities.Endpoint"]

    class NetworkLinksTo(DependsOn):
        _tosca_name = "tosca.relationships.network.LinksTo"
        _valid_target_types = ["capabilities.NetworkLinkable"]

    class NetworkBindsTo(DependsOn):
        _tosca_name = "tosca.relationships.network.BindsTo"
        _valid_target_types = ["capabilities.NetworkBindable"]


class nodes(Namespace):
    class Root(NodeType, interfaces.NodeLifecycleStandard):
        """
        The TOSCA root node all other TOSCA base node types derive from.
        """

        _tosca_name = "tosca.nodes.Root"
        tosca_id: str = Attribute()
        tosca_name: str = Attribute()
        state: str = Attribute()

        feature: "capabilities.Node" = Capability(factory=capabilities.Node)

        dependency: Sequence[
            Union["relationships.DependsOn", "nodes.Root", "capabilities.Node"]
        ] = Requirement(default=())

    class AbstractCompute(Root):
        _tosca_name = "tosca.nodes.Abstract.Compute"
        host: "capabilities.Compute" = Capability(factory=capabilities.Compute)

    class Compute(AbstractCompute):
        _tosca_name = "tosca.nodes.Compute"
        private_address: str = Attribute()
        public_address: str = Attribute()
        networks: Dict[str, "datatypes.NetworkNetworkInfo"] = Attribute()
        ports: Dict[str, "datatypes.NetworkPortInfo"] = Attribute()

        host: "capabilities.Compute" = Capability(
            factory=capabilities.Compute,
            valid_source_types=["tosca.nodes.SoftwareComponent"],
        )

        binding: "capabilities.NetworkBindable" = Capability(
            factory=capabilities.NetworkBindable
        )

        os: "capabilities.OperatingSystem" = Capability(
            factory=capabilities.OperatingSystem
        )

        scalable: "capabilities.Scalable" = Capability(factory=capabilities.Scalable)

        endpoint: "capabilities.EndpointAdmin" = Capability(
            factory=capabilities.EndpointAdmin
        )

        local_storage: Sequence[
            Union[
                "relationships.AttachesTo",
                "nodes.StorageBlockStorage",
                "capabilities.Attachment",
            ]
        ] = Requirement(default=())

    class SoftwareComponent(Root):
        _tosca_name = "tosca.nodes.SoftwareComponent"
        component_version: Union[tosca_version, None] = None
        """
       Software component version.
       """
        admin_credential: Union["datatypes.Credential", None] = None

        host: Sequence[
            Union["relationships.HostedOn", "nodes.Compute", "capabilities.Compute"]
        ] = Requirement(default=())

    class DBMS(SoftwareComponent):
        _tosca_name = "tosca.nodes.DBMS"
        port: Union[int, None] = None
        """
       The port the DBMS service will listen to for data and requests.
       """
        root_password: Union[str, None] = None
        """
       The root password for the DBMS service.
       """

        host_capability: "capabilities.Compute" = Capability(
            name="host",
            factory=capabilities.Compute,
            valid_source_types=["tosca.nodes.Database"],
        )

    class Database(Root):
        _tosca_name = "tosca.nodes.Database"
        user: Union[str, None] = None
        """
       User account name for DB administration
       """
        port: Union[int, None] = None
        """
       The port the database service will use to listen for incoming data and requests.
       """
        name: Union[str, None] = None
        """
       The name of the database.
       """
        password: Union[str, None] = None
        """
       The password for the DB user account
       """

        database_endpoint: "capabilities.EndpointDatabase" = Capability(
            factory=capabilities.EndpointDatabase
        )

        host: Union[
            "relationships.HostedOn", "nodes.DBMS", "capabilities.Compute"
        ] = Requirement()

    class WebServer(SoftwareComponent):
        _tosca_name = "tosca.nodes.WebServer"
        data_endpoint: "capabilities.Endpoint" = Capability(
            factory=capabilities.Endpoint
        )

        admin_endpoint: "capabilities.EndpointAdmin" = Capability(
            factory=capabilities.EndpointAdmin
        )

        host_capability: "capabilities.Compute" = Capability(
            name="host",
            factory=capabilities.Compute,
            valid_source_types=["tosca.nodes.WebApplication"],
        )

    class WebApplication(Root):
        _tosca_name = "tosca.nodes.WebApplication"
        context_root: Union[str, None] = None

        app_endpoint: "capabilities.Endpoint" = Capability(
            factory=capabilities.Endpoint
        )

        host: Union[
            "relationships.HostedOn", "nodes.WebServer", "capabilities.Compute"
        ] = Requirement()

    class AbstractStorage(Root):
        _tosca_name = "tosca.nodes.Abstract.Storage"
        name: str
        size: Annotated[Size, (greater_or_equal(0),)] = 0 * MB

    class StorageBlockStorage(AbstractStorage):
        _tosca_name = "tosca.nodes.Storage.BlockStorage"
        volume_id: Union[str, None] = Property(attribute=True, default=None)
        snapshot_id: Union[str, None] = Property(attribute=True, default=None)

        attachment: "capabilities.Attachment" = Capability(
            factory=capabilities.Attachment
        )

    class BlockStorage(StorageBlockStorage):
        _tosca_name = "tosca.nodes.BlockStorage"
        name: str = ""

    class StorageObjectStorage(AbstractStorage):
        _tosca_name = "tosca.nodes.Storage.ObjectStorage"
        maxsize: Annotated[Size, (greater_or_equal(0),)]

        storage_endpoint: "capabilities.Endpoint" = Capability(
            factory=capabilities.Endpoint
        )

    class NetworkNetwork(Root):
        """
        The TOSCA Network node represents a simple, logical network service.
        """

        _tosca_name = "tosca.nodes.network.Network"
        ip_version: Union[Annotated[int, (valid_values([4, 6]),)], None] = 4
        """
       The IP version of the requested network. Valid values are 4 for ipv4 or 6 for ipv6.
       """
        cidr: Union[str, None] = None
        """
       The cidr block of the requested network.
       """
        start_ip: Union[str, None] = None
        """
       The IP address to be used as the start of a pool of addresses within the full IP range derived from the cidr block.
       """
        end_ip: Union[str, None] = None
        """
       The IP address to be used as the end of a pool of addresses within the full IP range derived from the cidr block.
       """
        gateway_ip: Union[str, None] = None
        """
       The gateway IP address.
       """
        network_name: Union[str, None] = None
        """
       An identifier that represents an existing Network instance in the underlying cloud infrastructure or can be used as the name of the newly created network. If network_name is provided and no other properties are provided (with exception of network_id), then an existing network instance will be used. If network_name is provided alongside with more properties then a new network with this name will be created.
       """
        network_id: Union[str, None] = None
        """
       An identifier that represents an existing Network instance in the underlying cloud infrastructure. This property is mutually exclusive with all other properties except network_name. This can be used alone or together with network_name to identify an existing network.
       """
        segmentation_id: Union[str, None] = None
        """
       A segmentation identifier in the underlying cloud infrastructure. E.g. VLAN ID, GRE tunnel ID, etc..
       """
        network_type: Union[str, None] = None
        """
       It specifies the nature of the physical network in the underlying cloud infrastructure. Examples are flat, vlan, gre or vxlan. For flat and vlan types, physical_network should be provided too.
       """
        physical_network: Union[str, None] = None
        """
       It identifies the physical network on top of which the network is implemented, e.g. physnet1. This property is required if network_type is flat or vlan.
       """
        dhcp_enabled: Union[bool, None] = True
        """
       Indicates should DHCP service be enabled on the network or not.
       """

        link: "capabilities.NetworkLinkable" = Capability(
            factory=capabilities.NetworkLinkable
        )

    class NetworkPort(Root):
        """
        The TOSCA Port node represents a logical entity that associates between Compute and Network normative types. The Port node type effectively represents a single virtual NIC on the Compute node instance.
        """

        _tosca_name = "tosca.nodes.network.Port"
        ip_address: Union[str, None] = Property(attribute=True, default=None)
        """
       Allow the user to set a static IP.
       """
        order: Union[Annotated[int, (greater_or_equal(0),)], None] = Property(
            attribute=True, default=0
        )
        """
       The order of the NIC on the compute instance (e.g. eth2).
       """
        is_default: Union[bool, None] = Property(attribute=True, default=False)
        """
       If is_default=true this port will be used for the default gateway route. Only one port that is associated to single compute node can set as is_default=true.
       """
        ip_range_start: Union[str, None] = Property(attribute=True, default=None)
        """
       Defines the starting IP of a range to be allocated for the compute instances that are associated with this Port.
       """
        ip_range_end: Union[str, None] = Property(attribute=True, default=None)
        """
       Defines the ending IP of a range to be allocated for the compute instances that are associated with this Port.
       """

        binding: Union[
            "relationships.NetworkBindsTo",
            "nodes.Compute",
            "capabilities.NetworkBindable",
        ] = Requirement()

        """
       Binding requirement expresses the relationship between Port and Compute nodes. Effectively it indicates that the Port will be attached to specific Compute node instance
       """
        link: Union[
            "relationships.NetworkLinksTo",
            "nodes.NetworkNetwork",
            "capabilities.NetworkLinkable",
        ] = Requirement()

        """
       Link requirement expresses the relationship between Port and Network nodes. It indicates which network this port will connect to.
       """

    class NetworkFloatingIP(Root):
        """
        The TOSCA FloatingIP node represents a floating IP that can associate to a Port.
        """

        _tosca_name = "tosca.nodes.network.FloatingIP"
        floating_network: str
        floating_ip_address: Union[str, None] = None
        port_id: Union[str, None] = None

        link: Union[
            "relationships.NetworkLinksTo",
            "nodes.NetworkPort",
            "capabilities.NetworkLinkable",
        ] = Requirement()

    class ObjectStorage(Root):
        """
        The TOSCA ObjectStorage node represents storage that provides the ability to store data as objects (or BLOBs of data) without consideration for the underlying filesystem or devices
        """

        _tosca_name = "tosca.nodes.ObjectStorage"
        name: str
        """
       The logical name of the object store (or container).
       """
        size: Union[Annotated[Size, (greater_or_equal(0),)], None] = None
        """
       The requested initial storage size.
       """
        maxsize: Union[Annotated[Size, (greater_or_equal(0),)], None] = None
        """
       The requested maximum storage size.
       """

        storage_endpoint: "capabilities.Endpoint" = Capability(
            factory=capabilities.Endpoint
        )

    class LoadBalancer(Root):
        _tosca_name = "tosca.nodes.LoadBalancer"
        algorithm: Union[str, None] = Property(status="experimental", default=None)

        client: Sequence["capabilities.EndpointPublic"] = Capability(default=())

        """the Floating (IP) client’s on the public network can connect to"""
        application: Sequence[
            Union["relationships.RoutesTo", "capabilities.Endpoint"]
        ] = Requirement(default=())

        """Connection to one or more load balanced applications"""

    class ContainerApplication(Root):
        _tosca_name = "tosca.nodes.Container.Application"
        host: Union[
            "relationships.HostedOn", "nodes.ContainerRuntime", "capabilities.Container"
        ] = Requirement()

        storage: "capabilities.Storage"
        network: "capabilities.Endpoint"

    class ContainerRuntime(SoftwareComponent):
        _tosca_name = "tosca.nodes.Container.Runtime"
        host_capability: "capabilities.Container" = Capability(
            name="host",
            factory=capabilities.Container,
            valid_source_types=["tosca.nodes.Container.Application"],
        )

        scalable: "capabilities.Scalable" = Capability(factory=capabilities.Scalable)

    class ContainerRuntimeDocker(ContainerRuntime):
        _tosca_name = "tosca.nodes.Container.Runtime.Docker"
        host_capability: "capabilities.ContainerDocker" = Capability(
            name="host",
            factory=capabilities.ContainerDocker,
            valid_source_types=["tosca.nodes.Container.Application.Docker"],
        )

    class ContainerApplicationDocker(ContainerApplication):
        _tosca_name = "tosca.nodes.Container.Application.Docker"
        host: "capabilities.ContainerDocker"


class groups(Namespace):
    class Root(GroupType, interfaces.NodeLifecycleStandard):
        """The TOSCA Group Type all other TOSCA Group Types derive from"""

        _tosca_name = "tosca.groups.Root"


class policies(Namespace):
    class Root(PolicyType):
        """The TOSCA Policy Type all other TOSCA Policy Types derive from."""

        _tosca_name = "tosca.policies.Root"

    class Placement(Root):
        """The TOSCA Policy Type definition that is used to govern placement of TOSCA nodes or groups of nodes."""

        _tosca_name = "tosca.policies.Placement"

    class Scaling(Root):
        """The TOSCA Policy Type definition that is used to govern scaling of TOSCA nodes or groups of nodes."""

        _tosca_name = "tosca.policies.Scaling"

    class Monitoring(Root):
        """The TOSCA Policy Type definition that is used to govern monitoring of TOSCA nodes or groups of nodes."""

        _tosca_name = "tosca.policies.Monitoring"

    class Update(Root):
        """The TOSCA Policy Type definition that is used to govern update of TOSCA nodes or groups of nodes."""

        _tosca_name = "tosca.policies.Update"

    class Performance(Root):
        """The TOSCA Policy Type definition that is used to declare performance requirements for TOSCA nodes or groups of nodes."""

        _tosca_name = "tosca.policies.Performance"

    class Reservation(Root):
        """The TOSCA Policy Type definition that is used to create TOSCA nodes or group of nodes based on the reservation."""

        _tosca_name = "tosca.policies.Reservation"

