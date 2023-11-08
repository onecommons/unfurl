# Generated by tosca.yaml2python from unfurl/tosca_plugins/artifacts.yaml at 2023-11-07T03:07:17 overwrite not modified (change to "ok" to allow)

import unfurl
from typing import List, Dict, Any, Tuple, Union, Sequence
from typing_extensions import Annotated
from tosca import (
    ArtifactType,
    Attribute,
    B,
    BPS,
    Bitrate,
    Capability,
    CapabilityType,
    D,
    DataType,
    Eval,
    Frequency,
    GB,
    GBPS,
    GHZ,
    GHz,
    GIB,
    GIBPS,
    Gbps,
    GiB,
    Gibps,
    GroupType,
    H,
    HZ,
    Hz,
    InterfaceType,
    KB,
    KBPS,
    KHZ,
    KIB,
    KIBPS,
    Kbps,
    KiB,
    Kibps,
    M,
    MB,
    MBPS,
    MHZ,
    MHz,
    MIB,
    MIBPS,
    MS,
    Mbps,
    MiB,
    Mibps,
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
    TIB,
    TIBPS,
    Tbps,
    TiB,
    Tibps,
    Time,
    ToscaDataType,
    ToscaInputs,
    ToscaOutputs,
    US,
    b,
    bps,
    d,
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
    hz,
    in_range,
    kB,
    kHz,
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
    max_length,
    mb,
    mbps,
    metadata_to_yaml,
    mhz,
    mib,
    mibps,
    min_length,
    ms,
    ns,
    operation,
    pattern,
    s,
    tb,
    tbps,
    tib,
    tibps,
    tosca_timestamp,
    tosca_version,
    us,
    valid_values,
)
import tosca
import unfurl.configurators.terraform
import unfurl.configurators


class artifact_AsdfTool(unfurl.artifacts.ShellExecutable):
    _type_name = "artifact.AsdfTool"
    version: Union[str, None] = "latest"

    @operation(apply_to=["Standard.configure", "Standard.delete"])
    def default(self, **kw):
        return unfurl.configurators.CmdConfigurator(
            cmd=Eval(
                (
                    'if ! [ -x "$(command -v asdf)" ]; then\n'
                    '  ASDF_REPO={{ "asdf" | get_dir }}\n'
                    '  export ASDF_DATA_DIR="${ASDF_DATA_DIR:-$ASDF_REPO}"\n'
                    "  source $ASDF_REPO/asdf.sh\n"
                    "fi\n"
                    "asdf plugin add {{SELF.file}}\n"
                    "asdf {%if task.operation == 'delete' %}uninstall{%else%}install{%endif%} "
                    "{{SELF.file}} {{SELF.version}}\n"
                    "{%if task.operation != 'delete' %}asdf local {{SELF.file}} "
                    "{{SELF.version}}{%endif%}\n"
                )
            ),
            cwd=Eval('{{ "project" | get_dir }}'),
            keeplines=True,
            shell=Eval('{{ "bash" | which }}'),
            resultTemplate=Eval(
                {
                    "to_env": {
                        "^PATH": '{{ lookup("env", "ASDF_DATA_DIR") }}/installs/{{ '
                        "SELF.file}}/{{SELF.version}}/bin }}"
                    },
                    "update_os_environ": True,
                }
            ),
        )


class artifact_PythonPackage(tosca.artifacts.Root):
    _type_name = "artifact.PythonPackage"
    version: str = ""
    """Full version specifier (e.g. "==1.0")"""

    def check(self, **kw):
        return unfurl.configurators.PythonPackageCheckConfigurator()

    def create(self, **kw):
        return unfurl.configurators.CmdConfigurator(
            cmd=Eval(
                (
                    "{%if lookup('env', 'VIRTUAL_ENV') %}pipenv --bare{% else %}{{ "
                    "__python_executable }} -m pip{% endif %} install "
                    "'{{SELF.file}}{{SELF.version}}'"
                )
            ),
        )

    def delete(self, **kw):
        return unfurl.configurators.CmdConfigurator(
            cmd=Eval(
                (
                    "{%if lookup('env', 'VIRTUAL_ENV') %}pipenv --bare{% else %}{{ "
                    "__python_executable }} -m pip{% endif %} uninstall "
                    "'{{SELF.file}}{{SELF.version}}'"
                )
            ),
        )


class artifact_AnsibleCollection(tosca.artifacts.Root):
    _type_name = "artifact.AnsibleCollection"
    version: Union[str, None] = None

    def check(self, **kw):
        return unfurl.configurators.CmdConfigurator(
            cmd=Eval("ansible-galaxy collection list --format json {{SELF.file}}"),
            done={"success": True},
            resultTemplate=Eval(
                (
                    "{%if returncode == 0 and stdout | from_json %}\n"
                    "readyState: ok\n"
                    "{% else %}\n"
                    "readyState: absent\n"
                    "{% endif %} \n"
                )
            ),
        )

    def create(self, **kw):
        return unfurl.configurators.CmdConfigurator(
            cmd=Eval("ansible-galaxy collection install {{SELF.file}}"),
        )

    def delete(self, **kw):
        return unfurl.configurators.CmdConfigurator(
            cmd=Eval(
                (
                    "rm -rf {{ lookup('env', 'COLLECTIONS_PATHS' }.split(':')[0] }}/{{ "
                    "SELF.file.replace('.', '/') }}"
                )
            ),
        )


class unfurl_nodes_Installer_Terraform(unfurl.nodes.Installer):
    _type_name = "unfurl.nodes.Installer.Terraform"
    main: Union[str, None] = Property(metadata={"user_settable": False}, default=None)

    @operation(apply_to=["Install.check", "Standard.delete"])
    def default(self, **kw):
        return unfurl.configurators.terraform.TerraformConfigurator(
            main=Eval({"get_property": ["SELF", "main"]}),
        )


configurator_artifacts = unfurl.nodes.LocalRepository(
    "configurator-artifacts",
    _directives=["default"],
)
configurator_artifacts.terraform = artifact_AsdfTool(
    "terraform",
    version="1.1.4",
    file="terraform",
)
configurator_artifacts.gcloud = artifact_AsdfTool(
    "gcloud",
    version="398.0.0",
    file="gcloud",
)
configurator_artifacts.kompose = artifact_AsdfTool(
    "kompose",
    version="v1.26.1",
    file="kompose",
)
configurator_artifacts.google_auth = artifact_PythonPackage(
    "google-auth",
    file="google-auth",
)
configurator_artifacts.octodns = artifact_PythonPackage(
    "octodns",
    version="==0.9.14",
    file="octodns",
)
configurator_artifacts.kubernetes_core = artifact_AnsibleCollection(
    "kubernetes.core",
    version="2.4.0",
    file="kubernetes.core",
)
configurator_artifacts.community_docker = artifact_AnsibleCollection(
    "community.docker",
    version="1.10.2",
    file="community.docker",
)
configurator_artifacts.ansible_utils = artifact_AnsibleCollection(
    "ansible.utils",
    version="2.10.3",
    file="ansible.utils",
)

