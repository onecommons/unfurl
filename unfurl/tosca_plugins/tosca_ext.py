# Generated by tosca.yaml2python from unfurl/tosca_plugins/tosca-ext.yaml at 2023-09-19 15:58:50.358334

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
    Mapping,
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
import unfurl.configurators.gcp
import unfurl.configurators


class datatypes(Namespace):
    class EnvVar(tosca.datatypes.Root):
        """The value of an environment variable whose name matches the property's name"""

        _type_name = "unfurl.datatypes.EnvVar"
        _type = "string"


class artifacts(Namespace):
    class HasConfigurator(tosca.artifacts.Implementation):
        _type_name = "unfurl.artifacts.HasConfigurator"
        className: str
        """Name of the python class that implements the configurator interface"""

    class TemplateOperation(HasConfigurator):
        _type_name = "unfurl.artifacts.TemplateOperation"
        className: str = "unfurl.configurators.TemplateConfigurator"
        """Name of the python class that implements the configurator interface"""

    class ShellExecutable(HasConfigurator):
        _type_name = "unfurl.artifacts.ShellExecutable"
        className: str = "unfurl.configurators.shell.ShellConfigurator"
        """Name of the python class that implements the configurator interface"""

    class AnsiblePlaybook(HasConfigurator):
        _type_name = "unfurl.artifacts.AnsiblePlaybook"
        className: str = "unfurl.configurators.ansible.AnsibleConfigurator"
        """Name of the python class that implements the configurator interface"""


class capabilities(Namespace):
    class Installer(tosca.capabilities.Root):
        _type_name = "unfurl.capabilities.Installer"

    class EndpointAnsible(tosca.capabilities.EndpointAdmin):
        """Capability to connect to Ansible"""

        _type_name = "unfurl.capabilities.Endpoint.Ansible"
        connection: str = "local"
        """The connection type (sets "ansible_connection")"""

        port: Union[int, None] = None
        '''sets "ansible_port"'''

        host: Union[str, None] = None
        '''Sets "ansible_host"'''

        user: Union[str, None] = None
        """Sets "ansible_user" if not set in credentials"""

        authentication_type: Union[str, None] = None
        """Type of authentication required, should match the credential's token_type"""

        hostvars: Union[Dict, None] = None
        """
       Passed to ansible as host vars See https://docs.ansible.com/ansible/latest/user_guide/intro_inventory.html#connecting-to-hosts-behavioral-inventory-parameters
       """

    class EndpointSSH(EndpointAnsible):
        """Capability to connect to the host via SSH"""

        _type_name = "unfurl.capabilities.Endpoint.SSH"
        protocol: str = "ssh"
        connection: str = "ssh"
        """The connection type (sets "ansible_connection")"""

        port: Union[int, None] = 22
        '''sets "ansible_port"'''


class relationships(Namespace):
    class InstalledBy(tosca.relationships.Root):
        _type_name = "unfurl.relationships.InstalledBy"
        _valid_target_types = ["capabilities.Installer"]

    class Configures(tosca.relationships.Root):
        _type_name = "unfurl.relationships.Configures"

    class ConfiguringHostedOn(Configures, tosca.relationships.HostedOn):
        _type_name = "unfurl.relationships.ConfiguringHostedOn"

    class ConnectsToAnsible(tosca.relationships.ConnectsTo):
        _type_name = "unfurl.relationships.ConnectsTo.Ansible"
        credential: Union["tosca.datatypes.Credential", None] = Property(
            metadata={"sensitive": True}, default=None
        )
        '''Its "user" property sets "ansible_user", add properties like "ssh_private_key_file" to "keys"'''

        hostvars: Union[Dict, None] = None
        """
       Passed to ansible as host vars See https://docs.ansible.com/ansible/latest/user_guide/intro_inventory.html#connecting-to-hosts-behavioral-inventory-parameters
       """

        _valid_target_types = ["capabilities.EndpointAnsible"]

    class ConnectsToCloudAccount(tosca.relationships.ConnectsTo):
        _type_name = "unfurl.relationships.ConnectsTo.CloudAccount"

    class ConnectsToGoogleCloudProject(ConnectsToCloudAccount):
        _type_name = "unfurl.relationships.ConnectsTo.GoogleCloudProject"
        CLOUDSDK_CORE_PROJECT: Union[str, None] = Eval(
            {"get_env": "CLOUDSDK_CORE_PROJECT"}
        )
        """id of the project"""

        CLOUDSDK_COMPUTE_REGION: Union[str, None] = Eval(
            {"get_env": "CLOUDSDK_COMPUTE_REGION"}
        )
        """default region to use"""

        CLOUDSDK_COMPUTE_ZONE: Union[str, None] = Eval(
            {"get_env": "CLOUDSDK_COMPUTE_ZONE"}
        )
        """default zone to use"""

        GOOGLE_APPLICATION_CREDENTIALS: Union[str, None] = Eval(
            {"get_env": "GOOGLE_APPLICATION_CREDENTIALS"}
        )
        """Path to file containing service account private keys in JSON format"""

        GOOGLE_OAUTH_ACCESS_TOKEN: Union[str, None] = Eval(
            {"get_env": "GOOGLE_OAUTH_ACCESS_TOKEN"}
        )
        """A temporary OAuth 2.0 access token obtained from the Google Authorization server"""

        GCP_SERVICE_ACCOUNT_CONTENTS: Union[str, None] = Property(
            metadata={"sensitive": True},
            default=Eval({"get_env": "GCP_SERVICE_ACCOUNT_CONTENTS"}),
        )
        """Content of file containing service account private keys"""

        GCP_AUTH_KIND: Union[
            Annotated[
                str,
                (valid_values(["application", "machineaccount", "serviceaccount"]),),
            ],
            None,
        ] = Eval({"get_env": ["GCP_AUTH_KIND", "serviceaccount"]})
        scopes: Union[List[str], None] = None

        def check(self):
            return unfurl.configurators.gcp.CheckGoogleCloudConnectionConfigurator()

    class ConnectsToAWSAccount(ConnectsToCloudAccount):
        _type_name = "unfurl.relationships.ConnectsTo.AWSAccount"
        endpoints: Union[Dict, None] = None
        """custom service endpoints"""

        AWS_DEFAULT_REGION: Union[str, None] = Eval({"get_env": "AWS_DEFAULT_REGION"})
        """The default region to use, e.g. us-west-1, us-west-2, etc."""

        AWS_ACCESS_KEY_ID: Union[str, None] = Eval({"get_env": "AWS_ACCESS_KEY_ID"})
        """The access key for your AWS account"""

        AWS_SECRET_ACCESS_KEY: Union[str, None] = Property(
            metadata={"sensitive": True},
            default=Eval({"get_env": "AWS_SECRET_ACCESS_KEY"}),
        )
        """The secret key for your AWS account."""

        AWS_SESSION_TOKEN: Union[str, None] = Property(
            metadata={"sensitive": True}, default=Eval({"get_env": "AWS_SESSION_TOKEN"})
        )
        """The session key for your AWS account."""

        AWS_PROFILE: Union[str, None] = Eval({"get_env": "AWS_PROFILE"})
        AWS_SHARED_CREDENTIALS_FILE: Union[str, None] = Eval(
            {"get_env": "AWS_SHARED_CREDENTIALS_FILE"}
        )
        AWS_CONFIG_FILE: Union[str, None] = Eval({"get_env": "AWS_CONFIG_FILE"})

    class ConnectsToDigitalOcean(ConnectsToCloudAccount):
        _type_name = "unfurl.relationships.ConnectsTo.DigitalOcean"
        DIGITALOCEAN_TOKEN: str = Property(
            metadata={"user_settable": True, "sensitive": True},
            default=Eval({"get_env": "DIGITALOCEAN_TOKEN"}),
        )
        SPACES_ACCESS_KEY_ID: Union[str, None] = Property(
            metadata={"user_settable": True},
            default=Eval({"get_env": "SPACES_ACCESS_KEY_ID"}),
        )
        """The access key for Spaces object storage."""

        SPACES_SECRET_ACCESS_KEY: Union[str, None] = Property(
            metadata={"user_settable": True, "sensitive": True},
            default=Eval({"get_env": "SPACES_SECRET_ACCESS_KEY"}),
        )
        """The secret key for Spaces object storage."""

        default_region: str = Property(
            title="Default Region", metadata={"user_settable": True}, default="nyc1"
        )
        """The default region to use, e.g. fra1, nyc2, etc."""

    class ConnectsToAzure(ConnectsToCloudAccount):
        _type_name = "unfurl.relationships.ConnectsTo.Azure"
        AZURE_CLIENT_ID: Union[str, None] = Property(
            metadata={
                "env_vars": ["ARM_CLIENT_ID", "AZURE_CLIENT_ID"],
                "title": "Client ID",
                "user_settable": True,
            },
            default=Eval(
                {"get_env": ["ARM_CLIENT_ID", {"get_env": "AZURE_CLIENT_ID"}]}
            ),
        )
        """
       Also known as an Application ID or `appId`. Can be created via [CLI](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/guides/service_principal_client_certificate) or through the [Azure portal](https://learn.microsoft.com/azure/active-directory/develop/howto-create-service-principal-portal).
       """

        AZURE_TENANT: Union[str, None] = Property(
            metadata={
                "env_vars": ["ARM_TENANT_ID", "AZURE_TENANT"],
                "title": "Tenant",
                "user_settable": True,
            },
            default=Eval({"get_env": ["ARM_TENANT_ID", {"get_env": "AZURE_TENANT"}]}),
        )
        """
       [Find your Azure active directory tenant](https://learn.microsoft.com/en-us/azure/azure-portal/get-subscription-tenant-id#find-your-azure-ad-tenant)
       """

        AZURE_SUBSCRIPTION_ID: Union[str, None] = Property(
            metadata={
                "env_vars": ["ARM_SUBSCRIPTION_ID", "AZURE_SUBSCRIPTION_ID"],
                "title": "Azure Subscription",
                "user_settable": True,
            },
            default=Eval(
                {
                    "get_env": [
                        "ARM_SUBSCRIPTION_ID",
                        {"get_env": "AZURE_SUBSCRIPTION_ID"},
                    ]
                }
            ),
        )
        """
       [Find your Azure subscription](https://learn.microsoft.com/en-us/azure/azure-portal/get-subscription-tenant-id#find-your-azure-subscription)
       """

        AZURE_SECRET: Union[str, None] = Property(
            metadata={
                "env_vars": ["ARM_CLIENT_SECRET", "AZURE_SECRET"],
                "sensitive": True,
                "title": "Client Secret",
                "user_settable": True,
            },
            default=Eval(
                {"get_env": ["ARM_CLIENT_SECRET", {"get_env": "AZURE_SECRET"}]}
            ),
        )
        """
       For authentication with service principal. [(Portal link)](https://learn.microsoft.com/azure/active-directory/develop/howto-create-service-principal-portal#option-2-create-a-new-application-secret)
       """

        AZURE_AD_USER: Union[str, None] = Eval({"get_env": "AZURE_AD_USER"})
        """for authentication with Active Directory"""

        AZURE_PASSWORD: Union[str, None] = Property(
            metadata={"sensitive": True}, default=Eval({"get_env": "AZURE_PASSWORD"})
        )
        """for authentication with Active Directory"""

        AZURE_ADFS_AUTHORITY_URL: Union[str, None] = Eval(
            {"get_env": "AZURE_ADFS_AUTHORITY_URL"}
        )
        """set if you have your own ADFS authority"""

    class ConnectsToPacket(ConnectsToCloudAccount):
        _type_name = "unfurl.relationships.ConnectsTo.Packet"
        project: str
        """UUID to packet project"""

        PACKET_API_TOKEN: str = Property(
            metadata={"sensitive": True}, default=Eval({"get_env": "PACKET_API_TOKEN"})
        )

    class ConnectsToOpenStack(ConnectsToCloudAccount):
        _type_name = "unfurl.relationships.ConnectsTo.OpenStack"

    class ConnectsToRackspace(ConnectsToOpenStack):
        _type_name = "unfurl.relationships.ConnectsTo.Rackspace"


class nodes(Namespace):
    class Repository(tosca.nodes.Root):
        """
        Reification of a TOSCA repository. Artifacts listed in the "artifacts" section of this node template will able available in the repository.
        """

        _type_name = "unfurl.nodes.Repository"
        repository: Union[str, None] = None
        """The name of the repository this node instance represent."""

        url: Union[str, None] = None
        """The url of this repository"""

        credential: Union["tosca.datatypes.Credential", None] = Property(
            metadata={"sensitive": True}, default=None
        )
        """
       The credential, if present, of the repository this node instance represents.
       """

    class LocalRepository(Repository):
        """
        Represents the collection of artifacts available to the local operation host.
        """

        _type_name = "unfurl.nodes.LocalRepository"

    class ArtifactBuilder(tosca.nodes.Root):
        """
        Creates or builds the given artifact and "uploads" it to the artifact's repository.
        """

        _type_name = "unfurl.nodes.ArtifactBuilder"

    class ArtifactInstaller(tosca.nodes.Root):
        """
        Reification of an artifact that needs to be installed. Node templates of this type are "discovered" when artifacts need to be installed on an operation_host.
        """

        _type_name = "unfurl.nodes.ArtifactInstaller"

        @operation(
            apply_to=[
                "Install.check",
                "Standard.delete",
                "Standard.create",
                "Standard.configure",
                "Standard.start",
                "Standard.stop",
                "Mock.delete",
                "Mock.create",
                "Mock.configure",
                "Mock.start",
                "Mock.stop",
                "Mock.check",
            ]
        )
        def default(self):
            return unfurl.configurators.DelegateConfigurator(
                target=Eval({"eval": ".artifacts::install"}),
                inputs={},
            )

    class Installer(tosca.nodes.Root):
        _type_name = "unfurl.nodes.Installer"
        installer: "capabilities.Installer" = Capability(factory=capabilities.Installer)

    class Installation(tosca.nodes.Root):
        _type_name = "unfurl.nodes.Installation"
        installer: Union[
            Union[
                "relationships.InstalledBy", "nodes.Installer", "capabilities.Installer"
            ],
            None,
        ] = None

    class Default(Installation):
        """Used if pre-existing instances are declared with no TOSCA template"""

        _type_name = "unfurl.nodes.Default"

    class CloudAccount(tosca.nodes.Root):
        _type_name = "unfurl.nodes.CloudAccount"
        account_id: str = Attribute()
        """Cloud provider specific account identifier"""

    class CloudObject(tosca.nodes.Root):
        _type_name = "unfurl.nodes.CloudObject"
        uri: str = Attribute()
        """Unique identifier"""

        name: str = Attribute()
        """Human-friendly name of the resource"""

        console_url: Union[str, None] = Attribute()
        """URL for viewing this resource in its cloud provider's console"""

    class AWSAccount(CloudAccount):
        _type_name = "unfurl.nodes.AWSAccount"

    class AWSResource(CloudObject):
        _type_name = "unfurl.nodes.AWSResource"
        cloud: Union[
            Union["relationships.ConnectsToAWSAccount", "nodes.AWSAccount"], None
        ] = Requirement(default=None, metadata={"visibility": "hidden"})

    class AzureAccount(CloudAccount):
        _type_name = "unfurl.nodes.AzureAccount"

    class AzureResource(CloudObject):
        _type_name = "unfurl.nodes.AzureResource"
        cloud: Union[
            Union["relationships.ConnectsToAzure", "nodes.AzureAccount"], None
        ] = Requirement(default=None, metadata={"visibility": "hidden"})


class groups(Namespace):
    class AnsibleInventoryGroup(tosca.groups.Root):
        """Use this to place hosts in Ansible inventory groups"""

        _type_name = "unfurl.groups.AnsibleInventoryGroup"
        hostvars: Dict = Property(default_factory=lambda: ({}))
        """Ansible hostvars for members of this group"""


class interfaces(Namespace):
    # this is already defined because tosca.nodes.Root needs to inherit from it
    Install = tosca.interfaces.Install


if __name__ == "__main__":
    tosca.dump_yaml(globals())

