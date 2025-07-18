# Generated by tosca.yaml2python from unfurl/configurators/templates/helm.yaml at 2025-06-18T16:48:16 overwrite not modified (change to "overwrite ok" to allow)

import unfurl
from typing import List, Dict, Any, Tuple, Union, Sequence
import tosca
from tosca import Capability, Eval, Interface, Node, max_length, operation
import typing_extensions
import unfurl.configurators.shell
from unfurl.tosca_plugins.artifacts import *
from unfurl.tosca_plugins.k8s import *


class unfurl_interfaces_Helm(tosca.interfaces.Root):
    _type_name = "unfurl.interfaces.Helm"
    execute = operation()

    _Helm_default_inputs = {
        "helmcmd": {"type": "string"},
        "release_name": {"type": "string"},
        "chart": {"type": "string", "required": False},
        "flags": {"type": "map", "required": False},
        "chart_values": {"type": "map", "required": False},
    }


class unfurl_nodes_HelmRepository(tosca.nodes.Root):
    """Represents a Helm repository"""

    _type_name = "unfurl.nodes.HelmRepository"
    name: str = Eval({"eval": ".name"})
    url: str

    def check(self, **kw: Any) -> Any:
        return unfurl.configurators.shell.ShellConfigurator(
            command=Eval(
                "helm repo list -o json {% if task.verbose > 0 %}--debug{%endif%}"
            ),
            done={"success": True},
            resultTemplate=Eval(
                r"""\
- readyState: absent
{%if returncode == 0 %}
  {%for json in stdout | from_json %}
    {%if json.name == SELF.name %}
- readyState: {%if json.url == json.url %}ok{%else%}error{%endif%}
    {%endif%}
  {% endfor%}
{%endif%}
"""
            ),
        )

    def discover(self, **kw: Any) -> Any:
        return unfurl.configurators.shell.ShellConfigurator(
            command=Eval(
                "helm repo list -o json {% if task.verbose > 0 %}--debug{%endif%}"
            ),
            done={"success": True, "modified": False},
            resultTemplate=Eval(
                r"""\
{%if returncode == 0 %}
  {%for json in stdout | from_json %}
    - name: {{ json.name }}-helm-repo
      template:
        type: unfurl.nodes.HelmRepository
        properties:
          name: {{ json.name }}
          url: {{ json.url }}
      readyState: ok
  {% endfor%}
{%endif%}
"""
            ),
        )

    def configure(self, **kw: Any) -> Any:
        return unfurl.configurators.shell.ShellConfigurator(
            command=Eval(
                'helm repo add {{ SELF.name }} "{{ SELF.url }}" --force-update'
            ),
        )

    def delete(self, **kw: Any) -> Any:
        return unfurl.configurators.shell.ShellConfigurator(
            command=Eval("helm repo remove {{ SELF.name }}"),
        )


class unfurl_nodes_HelmRelease(unfurl.nodes.Installation, unfurl_interfaces_Helm):
    """Represents a Helm release"""

    _type_name = "unfurl.nodes.HelmRelease"
    chart: str
    version: Union[str, None] = None
    """chart version"""

    chart_values: Union[Dict[str, Any], None] = None
    record_resources: bool = True
    """Save kubernetes resources created by the Helm release as managed instances."""

    release_name: typing_extensions.Annotated[str, (max_length(53),)] = Eval(
        {"eval": {"to_dns_label": {"eval": ".name"}, "max": 53}}
    )
    """name of the helm release"""

    namespace: Union[str, None] = Eval(
        {"eval": ".::.requirements::[.name=host]::.target::name"}
    )

    host: Union[
        Union["tosca.relationships.HostedOn", "unfurl_nodes_K8sNamespace"], None
    ] = None
    repository: Union["unfurl_nodes_HelmRepository", None] = None

    def check(self, **kw: Any) -> Any:
        return self.execute(
            done=Eval(
                {
                    "success": '{%if result.returncode == 0 or "release: not found" in '
                    "result.stderr %}True{%else%}False{%endif%}",
                    "modified": False,
                }
            ),
            resultTemplate=Eval(
                r"""\
{%if returncode == 0 %}
{% set json = stdout | from_json %}
- readyState: {%if json.info.status == 'deleted' or json.info.status == 'deleting' %}absent
              {%- elif json.info.status=='superseded' %}degraded
              {%- elif json.info.status=='deployed' %}ok
              {%- elif json.info.status=='failed' %}error
              {%- else %}ok{%- endif%}
{% elif "release: not found" in stderr %}
- readyState: absent
{% endif %}
"""
            ),
            helmcmd="status",
            dryrun="--dry-run",
            chart="",
            chart_values="",
        )

    def discover(self, **kw: Any) -> Any:
        return self.execute(
            helmcmd="list",
            dryrun="--dry-run",
            chart="",
            chart_values="",
            resultTemplate=Eval(
                r"""\
{%if returncode == 0 %}
  {%for json in stdout | from_json %}
    - name: {{ json.name }}-release
      template:
        type: unfurl.nodes.HelmRelease
        properties:
          chart: {{ json.chart }}
          release_name: {{ json.name }}
      readyState: unknown
  {% endfor%}
{%endif%}
"""
            ),
        )

    def configure(self, **kw: Any) -> Any:
        return self.execute(
            helmcmd=Eval(
                (
                    'upgrade --install {%if SELF.version | default("") %}--version '
                    "{{SELF.version}}{% endif %}"
                )
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
    def delete(self, **kw: Any) -> Any:
        return unfurl.configurators.shell.ShellConfigurator(
            command=Eval(
                {
                    "eval": {
                        "template": r"""\
helm uninstall {{ "release_name" | eval }}
  {% if SELF.namespace %}--namespace {{ SELF.namespace }}{% endif -%}
  {% if task.verbose > 0 %} --debug{%endif -%}
  {% if task.timeout %} --timeout {{task.timeout}}{%endif -%}
  {% if inputs.flags is defined -%}
    {% for flag, value in inputs.flags.items() %}
      --{{flag}} {%if value is not none %}"{{ value | quote }}"{% endif %}
    {% endfor%}
  {%endif%}
"""
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
    def execute(self, **kw: Any) -> Any:
        return unfurl.configurators.shell.ShellConfigurator(
            command=Eval(
                {
                    "eval": {
                        "template": r"""\
helm {{inputs.helmcmd}} {{inputs.release_name}} {{inputs.chart }} -o json
  {% if SELF.namespace %}--namespace {{ SELF.namespace }}{% endif %}
  {%if lookup('env', 'KUBE_INSECURE') %}
  --kube-insecure-skip-tls-verify
  {% endif %}
  {% if inputs.chart_values | default('', true) %}
  --values {{ valuesfile }}
  {% endif %}
  {% if task.verbose > 0 %}--debug{%endif%}
  {% if task.timeout %}--timeout {{task.timeout}}{%endif%}
  {% if 'upgrade' in inputs.helmcmd %}--reuse-values{%endif%}
  {% if inputs.flags is defined -%}
    {% for flag, value in inputs.flags.items() %}
      --{{flag}} {%if value is not none %}"{{ value | quote }}"{% endif %}
    {% endfor%}
  {%endif%}
"""
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
            echo=False,
            resultTemplate=Eval(
                r"""\
{%if returncode == 0 and SELF.record_resources %}
{% set json = stdout | from_json %}
{%for doc in json.manifest | from_yaml_all %}
  {%if doc.kind is defined and doc.kind != 'Secret' %}
  - name: {{doc.kind}}-{{doc.metadata.namespace | default('') }}-{{ doc.metadata.name }}
    {%if doc.metadata.namespace is not defined %}
    parent: HOST
    {% endif %}
    template:
      {%if doc.kind == 'Secret' %}
      type: unfurl.nodes.K8sSecretResource
      {% else %}
      type: unfurl.nodes.K8sResource
      {% endif %}{%if doc.metadata.namespace is defined %}
      requirements:
        - host:
            node: {{ "::*::[.template::type=unfurl.nodes.K8sNamespace][name=$namespace]::.template::name" | eval(namespace=doc.metadata.namespace) }}
      {%endif%}
    attributes:
      {%if doc.metadata.namespace is defined %}
      namespace: {{doc.metadata.namespace }}
      {% endif %}
      apiResource:
{{ doc | to_yaml | indent(10, true) }}
    readyState: {%if json.info.status == 'deleted' or json.info.status == 'deleting' %}absent
                {%- elif json.info.status=='superseded' %}degraded
                {%- elif json.info.status=='deployed' %}ok
                {%- elif json.info.status=='failed' %}error
                {%- else %}ok{%- endif%}
  {%endif%}
{% endfor %}
  - name: SELF
    readyState: ok
{% endif %}
"""
            ),
        )


helm_artifacts: Node = unfurl.nodes.LocalRepository(
    "helm-artifacts",
    _directives=["default"],
)
setattr(
    helm_artifacts,
    "helm",
    artifact_AsdfTool(
        "helm",
        file="helm",
        version="3.7.1",
    ),
)


dsl_definitions = {
    "environment": {
        "+HELM_KUBECONTEXT": {"eval": "$connections::K8sCluster::context"},
        "+HELM_KUBETOKEN": {"eval": "$connections::K8sCluster::token"},
        "+HELM_KUBEAPISERVER": {
            "eval": {
                "if": "$connections::K8sCluster::api_server",
                "then": {
                    "if": "{{ '//' "
                    "in "
                    "'$connections::K8sCluster::api_server' "
                    "| eval "
                    "}}",
                    "then": {"eval": "$connections::K8sCluster::api_server"},
                    "else": "{{ "
                    "'$connections::K8sCluster::protocol' "
                    "| "
                    "eval "
                    "}}://{{ "
                    "'$connections::K8sCluster::api_server' "
                    "| "
                    "eval "
                    "}}",
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
}
__all__ = [
    "unfurl_interfaces_Helm",
    "unfurl_nodes_HelmRepository",
    "unfurl_nodes_HelmRelease",
]

