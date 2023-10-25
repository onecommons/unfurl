# Generated by tosca.yaml2python from unfurl/configurators/templates/helm.yaml at 2023-10-24T18:46:44 overwrite not modified (change to "ok" to allow)

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
import unfurl.configurators.shell
from unfurl.tosca_plugins.artifacts import *
from unfurl.tosca_plugins.k8s import *


class unfurl_interfaces_Helm(tosca.interfaces.Root):
    _type_name = "unfurl.interfaces.Helm"

    def execute(self, **kw):
        pass

    @operation(apply_to=[])
    def default(self, **kw):
        pass


class unfurl_nodes_HelmRepository(tosca.nodes.Root):
    """Represents a Helm repository"""

    _type_name = "unfurl.nodes.HelmRepository"
    name: str = Eval({"eval": ".name"})
    url: str

    feature: "tosca.capabilities.Node" = Capability(factory=tosca.capabilities.Node)

    def check(self, **kw):
        return unfurl.configurators.shell.ShellConfigurator(
            command=Eval(
                "helm repo list -o json {% if task.verbose > 0 %}--debug{%endif%}"
            ),
            done={"success": True},
            resultTemplate=Eval(
                (
                    "- readyState: absent\n"
                    "{%if returncode == 0 %}\n"
                    "  {%for json in stdout | from_json %}\n"
                    "    {%if json.name == SELF.name %}\n"
                    "- readyState: {%if json.url == json.url %}ok{%else%}error{%endif%}\n"
                    "    {%endif%}\n"
                    "  {% endfor%}\n"
                    "{%endif%}\n"
                )
            ),
        )

    def discover(self, **kw):
        return unfurl.configurators.shell.ShellConfigurator(
            command=Eval(
                "helm repo list -o json {% if task.verbose > 0 %}--debug{%endif%}"
            ),
            done={"success": True, "modified": False},
            resultTemplate=Eval(
                (
                    "{%if returncode == 0 %}\n"
                    "  {%for json in stdout | from_json %}\n"
                    "    - name: {{ json.name }}-helm-repo\n"
                    "      template:\n"
                    "        type: unfurl.nodes.HelmRepository\n"
                    "        properties:\n"
                    "          name: {{ json.name }}\n"
                    "          url: {{ json.url }}\n"
                    "      readyState: ok\n"
                    "  {% endfor%}\n"
                    "{%endif%}\n"
                )
            ),
        )

    def create(self, **kw):
        return unfurl.configurators.shell.ShellConfigurator(
            command=Eval('helm repo add {{ SELF.name }} "{{ SELF.url }}"'),
        )

    def delete(self, **kw):
        return unfurl.configurators.shell.ShellConfigurator(
            command=Eval("helm repo remove {{ SELF.name }}"),
        )


class unfurl_nodes_HelmRelease(unfurl.nodes.Installation, unfurl_interfaces_Helm):
    """Represents a Helm release"""

    _type_name = "unfurl.nodes.HelmRelease"
    chart: str
    chart_values: Union[Dict[str, Any], None] = None
    release_name: str
    namespace: Union[str, None] = Eval(
        {"eval": ".::.requirements::[.name=host]::.target::name"}
    )

    host: Union[
        Union["tosca.relationships.HostedOn", "unfurl_nodes_K8sNamespace"], None
    ] = None
    repository: Union["unfurl_nodes_HelmRepository", "tosca.capabilities.Node"]

    def check(self, **kw):
        return self.execute(
            done={"success": True, "modified": False},
            resultTemplate=Eval(
                "- readyState: {%if returncode == 0 %}ok{%else%}absent{%endif%}\n"
            ),
            helmcmd="status",
            dryrun="--dry-run",
            chart="",
            chart_values="",
        )

    def discover(self, **kw):
        return self.execute(
            helmcmd="list",
            dryrun="--dry-run",
            chart="",
            chart_values="",
            resultTemplate=Eval(
                (
                    "{%if returncode == 0 %}\n"
                    "  {%for json in stdout | from_json %}\n"
                    "    - name: {{ json.name }}-release\n"
                    "      template:\n"
                    "        type: unfurl.nodes.HelmRelease\n"
                    "        properties:\n"
                    "          chart: {{ json.chart }}\n"
                    "          release_name: {{ json.name }}\n"
                    "      readyState: unknown\n"
                    "  {% endfor%}\n"
                    "{%endif%}\n"
                )
            ),
        )

    def configure(self, **kw):
        return self.execute(
            helmcmd=Eval('{{ "upgrade" if ".::.present" | eval else "install"}}'),
        )

    @operation(
        environment=Eval(
            {
                "+HELM_KUBECONTEXT": {"eval": "$connections::K8sCluster::context"},
                "+HELM_KUBETOKEN": {"eval": "$connections::K8sCluster::token"},
                "+HELM_KUBEAPISERVER": {
                    "eval": {
                        "if": "$connections::K8sCluster::api_server",
                        "then": {
                            "if": "{{ '//' in "
                            "'$connections::K8sCluster::api_server' "
                            "| eval }}",
                            "then": {"eval": "$connections::K8sCluster::api_server"},
                            "else": "{{ "
                            "'$connections::K8sCluster::protocol' "
                            "| eval }}://{{ "
                            "'$connections::K8sCluster::api_server' "
                            "| eval }}",
                        },
                        "else": None,
                    }
                },
                "+HELM_KUBECAFILE": {
                    "eval": "$connections::K8sCluster::cluster_ca_certificate_file"
                },
                "+HELM_NAMESPACE": {"eval": "$connections::K8sCluster::namespace"},
                "+HELM_KUBEASGROUPS": {
                    "eval": {
                        "concat": {"eval": "$connections::K8sCluster::as_groups"},
                        "sep": ",",
                    }
                },
                "+HELM_KUBEASUSER": {"eval": "$connections::K8sCluster::as"},
            }
        )
    )
    def delete(self, **kw):
        return unfurl.configurators.shell.ShellConfigurator(
            command=Eval(
                {
                    "eval": {
                        "template": 'helm uninstall {{ "release_name" | eval }}\n'
                        "  {% if SELF.namespace %}--namespace {{ "
                        "SELF.namespace }}{% endif %}\n"
                        "  {% if task.verbose > 0 %}--debug{%endif%}\n"
                        "  {% if task.timeout %}--timeout "
                        "{{task.timeout}}{%endif%}\n"
                        "  {% if inputs.flags is defined -%}\n"
                        "    {% for flag, value in inputs.flags.items() %}\n"
                        '      --{{flag}} "{{value | quote }}"\n'
                        "    {% endfor%}\n"
                        "  {%endif%}"
                    }
                }
            ),
        )

    @operation(
        environment=Eval(
            {
                "+HELM_KUBECONTEXT": {"eval": "$connections::K8sCluster::context"},
                "+HELM_KUBETOKEN": {"eval": "$connections::K8sCluster::token"},
                "+HELM_KUBEAPISERVER": {
                    "eval": {
                        "if": "$connections::K8sCluster::api_server",
                        "then": {
                            "if": "{{ '//' in "
                            "'$connections::K8sCluster::api_server' "
                            "| eval }}",
                            "then": {"eval": "$connections::K8sCluster::api_server"},
                            "else": "{{ "
                            "'$connections::K8sCluster::protocol' "
                            "| eval }}://{{ "
                            "'$connections::K8sCluster::api_server' "
                            "| eval }}",
                        },
                        "else": None,
                    }
                },
                "+HELM_KUBECAFILE": {
                    "eval": "$connections::K8sCluster::cluster_ca_certificate_file"
                },
                "+HELM_NAMESPACE": {"eval": "$connections::K8sCluster::namespace"},
                "+HELM_KUBEASGROUPS": {
                    "eval": {
                        "concat": {"eval": "$connections::K8sCluster::as_groups"},
                        "sep": ",",
                    }
                },
                "+HELM_KUBEASUSER": {"eval": "$connections::K8sCluster::as"},
            }
        )
    )
    def execute(self, **kw):
        return unfurl.configurators.shell.ShellConfigurator(
            command=Eval(
                {
                    "eval": {
                        "template": "helm {{inputs.helmcmd}} {{inputs.release_name}} "
                        "{{inputs.chart }} -o json\n"
                        "  {% if SELF.namespace %}--namespace {{ "
                        "SELF.namespace }}{% endif %}\n"
                        "  {%if lookup('env', 'KUBE_INSECURE') %}\n"
                        "  --kube-insecure-skip-tls-verify\n"
                        "  {% endif %}\n"
                        "  {% if inputs.chart_values | default('', true) %}\n"
                        "  --values {{ valuesfile }}\n"
                        "  {% endif %}\n"
                        "  {% if task.verbose > 0 %}--debug{%endif%}\n"
                        "  {% if task.timeout %}--timeout "
                        "{{task.timeout}}{%endif%}\n"
                        "  {% if inputs.helmcmd == 'upgrade' "
                        "%}--reuse-values{%endif%}\n"
                        "  {% if inputs.flags is defined -%}\n"
                        "    {% for flag, value in inputs.flags.items() %}\n"
                        '      --{{flag}} "{{value | quote }}"\n'
                        "    {% endfor%}\n"
                        "  {%endif%}"
                    },
                    "vars": {
                        "valuesfile": {
                            "eval": {
                                "file": '{{ "values.yaml" | ' 'abspath("tasks") }}',
                                "contents": {"eval": "$inputs::chart_values"},
                            },
                            "select": "path",
                        }
                    },
                }
            ),
            chart=Eval({"get_property": ["SELF", "chart"]}),
            release_name=Eval({"get_property": ["SELF", "release_name"]}),
            dryrun="--dry-run",
            chart_values=Eval({"get_property": ["SELF", "chart_values"]}),
            resultTemplate=Eval(
                (
                    "{%if returncode == 0 %}\n"
                    "{% set json = stdout | from_json %}\n"
                    "{%for doc in json.manifest | from_yaml_all %}\n"
                    "  {%if doc.kind is defined and doc.kind != 'Secret' %}\n"
                    "  - name: {{doc.kind}}-{{doc.metadata.namespace | default('') }}-{{ "
                    "doc.metadata.name }}\n"
                    "    {%if doc.metadata.namespace is not defined %}\n"
                    "    parent: HOST\n"
                    "    {% endif %}\n"
                    "    template:\n"
                    "      {%if doc.kind == 'Secret' %}\n"
                    "      type: unfurl.nodes.K8sSecretResource\n"
                    "      {% else %}\n"
                    "      type: unfurl.nodes.K8sResource\n"
                    "      {% endif %}{%if doc.metadata.namespace is defined %}\n"
                    "      requirements:\n"
                    "        - host:\n"
                    "            node: {{ "
                    '"::*::[.template::type=unfurl.nodes.K8sNamespace][name=$namespace]::.template::name" '
                    "| eval(namespace=doc.metadata.namespace) }}\n"
                    "      {%endif%}\n"
                    "    attributes:\n"
                    "      {%if doc.metadata.namespace is defined %}\n"
                    "      namespace: {{doc.metadata.namespace }}\n"
                    "      {% endif %}\n"
                    "      apiResource:\n"
                    "{{ doc | to_yaml | indent(10, true) }}\n"
                    "    readyState: {%if json.info.status == 'deleted' or json.info.status == "
                    "'deleting' %}absent\n"
                    "                {%- elif json.info.status=='superseded' %}degraded\n"
                    "                {%- elif json.info.status=='deployed' %}ok\n"
                    "                {%- elif json.info.status=='failed' %}error\n"
                    "                {%- else %}ok{%- endif%}\n"
                    "  {%endif%}\n"
                    "{% endfor %}\n"
                    "  - name: SELF\n"
                    "    readyState: ok\n"
                    "{% endif %}\n"
                )
            ),
        )


helm_artifacts = unfurl.nodes.LocalRepository(
    "helm-artifacts",
    _directives=["default"],
)
helm_artifacts.helm = artifact_AsdfTool("helm", version="3.7.1", file="helm")

