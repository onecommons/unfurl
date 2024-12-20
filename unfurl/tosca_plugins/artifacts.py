# Generated by tosca.yaml2python from unfurl/tosca_plugins/artifacts.yaml at 2024-10-11T15:21:52 overwrite not modified (change to "overwrite ok" to allow)

import unfurl
from typing import List, Dict, Any, Tuple, Union, Sequence
import tosca
from tosca import ArtifactEntity, Eval, Node, Property, operation
import unfurl.configurators
import unfurl.configurators.terraform


class artifact_AsdfTool(unfurl.artifacts.ShellExecutable):
    _type_name = "artifact.AsdfTool"
    version: Union[str, None] = "latest"

    @operation(apply_to=["Standard.configure", "Standard.delete"])
    def default(self, **kw: Any) -> Any:
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
    version = ""
    """Full version specifier (e.g. "==1.0")"""

    def check(self, **kw: Any) -> Any:
        return unfurl.configurators.PythonPackageCheckConfigurator()

    def create(self, **kw: Any) -> Any:
        return unfurl.configurators.CmdConfigurator(
            cmd=Eval(
                (
                    "{%if lookup('env', 'VIRTUAL_ENV') %}pipenv --bare{% else %}{{ "
                    "__python_executable }} -m pip{% endif %} install "
                    "'{{SELF.file}}{{SELF.version}}'"
                )
            ),
        )

    def delete(self, **kw: Any) -> Any:
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

    def check(self, **kw: Any) -> Any:
        return unfurl.configurators.CmdConfigurator(
            cmd=Eval("{{__python_executable}} -m ansible.cli.galaxy collection list --format json {{SELF.file}}"),
            done={"success": True},
            resultTemplate=Eval(
                (
                    "{%if returncode == 0 and stdout | from_json %}\n"
                    "readyState: ok\n"
                    "# {{ {'python': 'unfurl.configurators.ansible.reload_collections'} | eval "
                    "}}\n"
                    "{% else %}\n"
                    "readyState: absent\n"
                    "{% endif %}\n"
                )
            ),
        )

    def create(self, **kw: Any) -> Any:
        return unfurl.configurators.CmdConfigurator(
            cmd=Eval("{{__python_executable}} -m ansible.cli.galaxy collection install {{SELF.file}}"),
            resultTemplate=Eval(
                {"eval": {"python": "unfurl.configurators.ansible.reload_collections"}}
            ),
        )


class unfurl_nodes_Installer_Terraform(unfurl.nodes.Installer):
    _type_name = "unfurl.nodes.Installer.Terraform"
    main: Union[str, None] = Property(metadata={"user_settable": False}, default=None)

    @operation(apply_to=["Install.check", "Standard.delete"])
    def default(self, **kw: Any) -> Any:
        return unfurl.configurators.terraform.TerraformConfigurator(
            main=Eval({"get_property": ["SELF", "main"]}),
        )


configurator_artifacts: Node = unfurl.nodes.LocalRepository(
    "configurator-artifacts",
    _directives=["default"],
)
__configurator_artifacts_terraform: ArtifactEntity = artifact_AsdfTool(
    "terraform",
    version="1.1.4",
    file="terraform",
)
configurator_artifacts.terraform = __configurator_artifacts_terraform  # type: ignore[attr-defined]
__configurator_artifacts_gcloud: ArtifactEntity = artifact_AsdfTool(
    "gcloud",
    version="499.0.0",
    file="gcloud",
)
configurator_artifacts.gcloud = __configurator_artifacts_gcloud  # type: ignore[attr-defined]
__configurator_artifacts_kompose: ArtifactEntity = artifact_AsdfTool(
    "kompose",
    version="1.26.1",
    file="kompose",
)
configurator_artifacts.kompose = __configurator_artifacts_kompose  # type: ignore[attr-defined]
__configurator_artifacts_google_auth: ArtifactEntity = artifact_PythonPackage(
    "google-auth",
    file="google-auth",
)
configurator_artifacts.google_auth = __configurator_artifacts_google_auth  # type: ignore[attr-defined]
__configurator_artifacts_octodns: ArtifactEntity = artifact_PythonPackage(
    "octodns",
    version="==0.9.14",
    file="octodns",
)
configurator_artifacts.octodns = __configurator_artifacts_octodns  # type: ignore[attr-defined]
__configurator_artifacts_kubernetes_core: ArtifactEntity = artifact_AnsibleCollection(
    "kubernetes.core",
    version="2.4.0",
    file="kubernetes.core",
)
configurator_artifacts.kubernetes_core = __configurator_artifacts_kubernetes_core  # type: ignore[attr-defined]
__configurator_artifacts_community_docker: ArtifactEntity = artifact_AnsibleCollection(
    "community.docker",
    version="3.12.1",
    file="community.docker",
)
configurator_artifacts.community_docker = __configurator_artifacts_community_docker  # type: ignore[attr-defined]
__configurator_artifacts_ansible_utils: ArtifactEntity = artifact_AnsibleCollection(
    "ansible.utils",
    version="2.10.3",
    file="ansible.utils",
)
configurator_artifacts.ansible_utils = __configurator_artifacts_ansible_utils  # type: ignore[attr-defined]


__all__ = [
    "artifact_AsdfTool",
    "artifact_PythonPackage",
    "artifact_AnsibleCollection",
    "unfurl_nodes_Installer_Terraform",
]

