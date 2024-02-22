# Generated by tosca.yaml2python from unfurl/configurators/templates/docker.yaml at 2024-02-18T10:26:42 overwrite not modified (change to "overwrite ok" to allow)

import unfurl
from typing import List, Dict, Any, Tuple, Union, Sequence
from typing_extensions import Annotated
from tosca import (
    Artifact,
    Attribute,
    AttributeOptions,
    CONSTRAINED,
    Capability,
    Computed,
    DEFAULT,
    DataType,
    Eval,
    MISSING,
    NodeType,
    Property,
    PropertyOptions,
    Requirement,
    ToscaInputs,
    ToscaOutputs,
    operation,
    valid_values,
)
import tosca
import unfurl.configurators.ansible
from unfurl.tosca_plugins.artifacts import *


class unfurl_datatypes_DockerContainer(DataType):
    _type_name = "unfurl.datatypes.DockerContainer"
    _type_metadata = {"additionalProperties": True}
    environment: Union["unfurl.datatypes.EnvironmentVariables", None] = Property(
        factory=lambda: (unfurl.datatypes.EnvironmentVariables())
    )
    container_name: Union[str, None] = None
    image: Union[str, None] = None
    command: Union[object, None] = None
    volumes: Union[List[str], None] = None
    ports: Union[List[str], None] = None
    """Ports to expose (format: 80:8080)"""

    user: Union[str, None] = None
    """User used to run the container process. format: <UID>[:<GID>]"""

    expose: Union[List[str], None] = None
    """Expose ports without publishing them to the host machine"""

    entrypoint: Union[List[str], None] = None
    privileged: Union[bool, None] = None
    pull_policy: Union[
        Annotated[str, (valid_values(["always", "never", "missing", "build"]),)], None
    ] = None
    network_mode: Union[str, None] = None
    """Use the same values as the docker client --network parameter ("bridge", "host", "none")"""


class unfurl_nodes_Container_Application_Docker(tosca.nodes.Root):
    _type_name = "unfurl.nodes.Container.Application.Docker"
    name: str = Eval({"eval": {"or": [".::container.container_name", ".name"]}})
    """The name of the container"""

    container: Union["unfurl_datatypes_DockerContainer", None] = None
    container_image: str = Eval(
        {
            "eval": {
                "if": ".artifacts::image",
                "then": {"get_artifact": ["SELF", "image"]},
                "else": {"eval": {"container_image": {"eval": "container::image"}}},
            }
        }
    )
    registry_url: Union[str, None] = Eval(
        "{{ '.::.artifacts::image::.repository::url' | eval }}"
    )
    registry_user: Union[str, None] = Eval(
        "{{ '.::.artifacts::image::.repository::credential::user' | eval }}"
    )
    registry_password: Union[str, None] = Eval(
        "{{ '.::.artifacts::image::.repository::credential::token' | eval }}"
    )

    image: Union[tosca.artifacts.DeploymentImageContainerDocker, None] = None

    @operation(outputs={"container": None, "image_path": None})
    def check(self, **kw: Any) -> Any:
        return unfurl.configurators.ansible.AnsibleConfigurator(
            playbook=Eval(
                {
                    "eval": {
                        "template": "#jinja2: variable_start_string: '<%', "
                        "variable_end_string: '%>'\n"
                        "{% filter from_yaml %}\n"
                        "{%if 'registry_user' | eval %}\n"
                        "- community.docker.docker_login:\n"
                        "     # "
                        "https://docs.ansible.com/ansible/latest/modules/docker_login_module.html#docker-login-module\n"
                        "     # "
                        "https://github.com/ansible/ansible/blob/stable-2.8/lib/ansible/modules/cloud/docker/docker_login.py\n"
                        '     username: "<% SELF.registry_user %>"\n'
                        '     password: "<% SELF.registry_password %>"\n'
                        '     registry_url: "<% SELF.registry_url %>"\n'
                        "{% endif %}\n"
                        "- set_fact:\n"
                        '    image_path: "<% SELF.container_image %>"\n'
                        "- community.docker.docker_container:\n"
                        "    # "
                        "https://docs.ansible.com/ansible/latest/collections/community/docker/docker_container_module.html#ansible-collections-community-docker-docker-container-module\n"
                        "    # "
                        "https://github.com/ansible/ansible/blob/stable-2.8/lib/ansible/modules/cloud/docker/docker_container.py\n"
                        '    name: "<% SELF.name %>" # required\n'
                        '    image: "{{ image_path }}" # Repository path and '
                        "tag\n"
                        '    state: "<%  inputs.state %>"\n'
                        "    {%if SELF.container is defined %}\n"
                        "    {%if SELF.container.pull_policy is defined "
                        "%}                    \n"
                        '    pull: <% SELF.container.pull_policy == "always" '
                        "%>\n"
                        "    {%endif%}\n"
                        "    # filter out env vars set to none\n"
                        "    env: <% SELF.container.environment | map_value | "
                        'dict2items | rejectattr("value", "none") | list | '
                        "items2dict | to_json %>\n"
                        "    # merge container dict after stripping out "
                        '"container_name" and "environment" keys\n'
                        "    <<: <% SELF.container | map_value | dict2items | "
                        'rejectattr("key", "equalto" , "container_name") | '
                        'rejectattr("key", "equalto" , "environment") | '
                        'rejectattr("key", "equalto" , "pull_policy") | list | '
                        "items2dict | to_json %>\n"
                        "    {%endif%}\n"
                        "    <<: <% inputs.configuration | default({}) | "
                        "map_value | to_json %>\n"
                        "    # XXX :\n"
                        "    # api_version: max(set(capabilities.versions) | "
                        "set(host::versions))\n"
                        "{% endfilter %}\n"
                    }
                }
            ),
            resultTemplate=Eval(
                (
                    '{% set status = outputs.container.State.Status | d("") %}\n'
                    '{% set error = outputs.container.State.Error | d("") %}\n'
                    "readyState:\n"
                    "  state: {{ {'created': 'created', 'restarting': 'starting', '': 'initial',\n"
                    "            'running': 'started', 'removing': 'deleting',\n"
                    "            'paused': 'stopped',  'stopped': 'stopped', 'exited': 'deleted', "
                    "'dead': 'deleted'}[status] }}\n"
                    "  local: {%if error %}error\n"
                    "              {% elif status == 'exited' or status == 'dead' %}absent\n"
                    "              {% elif status == 'running' %}ok\n"
                    "              {%else%}pending{%endif%}\n"
                    "# attributes: # XXX \n"
                    "#   container_image.digest:  outputs.container.Image\n"
                    "#   e.g. "
                    "sha256:a5ab4ab35b15731c675a531b85ec15c8dd50e36b22d96bcceeca37d016537c8e\n"
                )
            ),
            playbookArgs=["--check", "--diff"],
            state="started",
            done={"modified": False},
        )

    @operation(outputs={"container": None, "image_path": None})
    def configure(self, **kw: Any) -> Any:
        return unfurl.configurators.ansible.AnsibleConfigurator(
            playbook=Eval(
                {
                    "eval": {
                        "template": "#jinja2: variable_start_string: '<%', "
                        "variable_end_string: '%>'\n"
                        "{% filter from_yaml %}\n"
                        "{%if 'registry_user' | eval %}\n"
                        "- community.docker.docker_login:\n"
                        "     # "
                        "https://docs.ansible.com/ansible/latest/modules/docker_login_module.html#docker-login-module\n"
                        "     # "
                        "https://github.com/ansible/ansible/blob/stable-2.8/lib/ansible/modules/cloud/docker/docker_login.py\n"
                        '     username: "<% SELF.registry_user %>"\n'
                        '     password: "<% SELF.registry_password %>"\n'
                        '     registry_url: "<% SELF.registry_url %>"\n'
                        "{% endif %}\n"
                        "- set_fact:\n"
                        '    image_path: "<% SELF.container_image %>"\n'
                        "- community.docker.docker_container:\n"
                        "    # "
                        "https://docs.ansible.com/ansible/latest/collections/community/docker/docker_container_module.html#ansible-collections-community-docker-docker-container-module\n"
                        "    # "
                        "https://github.com/ansible/ansible/blob/stable-2.8/lib/ansible/modules/cloud/docker/docker_container.py\n"
                        '    name: "<% SELF.name %>" # required\n'
                        '    image: "{{ image_path }}" # Repository path and '
                        "tag\n"
                        '    state: "<%  inputs.state %>"\n'
                        "    {%if SELF.container is defined %}\n"
                        "    {%if SELF.container.pull_policy is defined "
                        "%}                    \n"
                        '    pull: <% SELF.container.pull_policy == "always" '
                        "%>\n"
                        "    {%endif%}\n"
                        "    # filter out env vars set to none\n"
                        "    env: <% SELF.container.environment | map_value | "
                        'dict2items | rejectattr("value", "none") | list | '
                        "items2dict | to_json %>\n"
                        "    # merge container dict after stripping out "
                        '"container_name" and "environment" keys\n'
                        "    <<: <% SELF.container | map_value | dict2items | "
                        'rejectattr("key", "equalto" , "container_name") | '
                        'rejectattr("key", "equalto" , "environment") | '
                        'rejectattr("key", "equalto" , "pull_policy") | list | '
                        "items2dict | to_json %>\n"
                        "    {%endif%}\n"
                        "    <<: <% inputs.configuration | default({}) | "
                        "map_value | to_json %>\n"
                        "    # XXX :\n"
                        "    # api_version: max(set(capabilities.versions) | "
                        "set(host::versions))\n"
                        "{% endfilter %}\n"
                    }
                }
            ),
            resultTemplate=Eval(
                (
                    '{% set status = outputs.container.State.Status | d("") %}\n'
                    '{% set error = outputs.container.State.Error | d("") %}\n'
                    "readyState:\n"
                    "  state: {{ {'created': 'created', 'restarting': 'starting', '': 'initial',\n"
                    "            'running': 'started', 'removing': 'deleting',\n"
                    "            'paused': 'stopped',  'stopped': 'stopped', 'exited': 'deleted', "
                    "'dead': 'deleted'}[status] }}\n"
                    "  local: {%if error %}error\n"
                    "              {% elif status == 'exited' or status == 'dead' %}absent\n"
                    "              {% elif status == 'running' %}ok\n"
                    "              {%else%}pending{%endif%}\n"
                    "# attributes: # XXX \n"
                    "#   container_image.digest:  outputs.container.Image\n"
                    "#   e.g. "
                    "sha256:a5ab4ab35b15731c675a531b85ec15c8dd50e36b22d96bcceeca37d016537c8e\n"
                )
            ),
            state="started",
        )

    @operation(outputs={"container": None, "image_path": None})
    def start(self, **kw: Any) -> Any:
        return unfurl.configurators.ansible.AnsibleConfigurator(
            playbook=Eval(
                {
                    "eval": {
                        "template": "#jinja2: variable_start_string: '<%', "
                        "variable_end_string: '%>'\n"
                        "{% filter from_yaml %}\n"
                        "{%if 'registry_user' | eval %}\n"
                        "- community.docker.docker_login:\n"
                        "     # "
                        "https://docs.ansible.com/ansible/latest/modules/docker_login_module.html#docker-login-module\n"
                        "     # "
                        "https://github.com/ansible/ansible/blob/stable-2.8/lib/ansible/modules/cloud/docker/docker_login.py\n"
                        '     username: "<% SELF.registry_user %>"\n'
                        '     password: "<% SELF.registry_password %>"\n'
                        '     registry_url: "<% SELF.registry_url %>"\n'
                        "{% endif %}\n"
                        "- set_fact:\n"
                        '    image_path: "<% SELF.container_image %>"\n'
                        "- community.docker.docker_container:\n"
                        "    # "
                        "https://docs.ansible.com/ansible/latest/collections/community/docker/docker_container_module.html#ansible-collections-community-docker-docker-container-module\n"
                        "    # "
                        "https://github.com/ansible/ansible/blob/stable-2.8/lib/ansible/modules/cloud/docker/docker_container.py\n"
                        '    name: "<% SELF.name %>" # required\n'
                        '    image: "{{ image_path }}" # Repository path and '
                        "tag\n"
                        '    state: "<%  inputs.state %>"\n'
                        "    {%if SELF.container is defined %}\n"
                        "    {%if SELF.container.pull_policy is defined "
                        "%}                    \n"
                        '    pull: <% SELF.container.pull_policy == "always" '
                        "%>\n"
                        "    {%endif%}\n"
                        "    # filter out env vars set to none\n"
                        "    env: <% SELF.container.environment | map_value | "
                        'dict2items | rejectattr("value", "none") | list | '
                        "items2dict | to_json %>\n"
                        "    # merge container dict after stripping out "
                        '"container_name" and "environment" keys\n'
                        "    <<: <% SELF.container | map_value | dict2items | "
                        'rejectattr("key", "equalto" , "container_name") | '
                        'rejectattr("key", "equalto" , "environment") | '
                        'rejectattr("key", "equalto" , "pull_policy") | list | '
                        "items2dict | to_json %>\n"
                        "    {%endif%}\n"
                        "    <<: <% inputs.configuration | default({}) | "
                        "map_value | to_json %>\n"
                        "    # XXX :\n"
                        "    # api_version: max(set(capabilities.versions) | "
                        "set(host::versions))\n"
                        "{% endfilter %}\n"
                    }
                }
            ),
            resultTemplate=Eval(
                (
                    '{% set status = outputs.container.State.Status | d("") %}\n'
                    '{% set error = outputs.container.State.Error | d("") %}\n'
                    "readyState:\n"
                    "  state: {{ {'created': 'created', 'restarting': 'starting', '': 'initial',\n"
                    "            'running': 'started', 'removing': 'deleting',\n"
                    "            'paused': 'stopped',  'stopped': 'stopped', 'exited': 'deleted', "
                    "'dead': 'deleted'}[status] }}\n"
                    "  local: {%if error %}error\n"
                    "              {% elif status == 'exited' or status == 'dead' %}absent\n"
                    "              {% elif status == 'running' %}ok\n"
                    "              {%else%}pending{%endif%}\n"
                    "# attributes: # XXX \n"
                    "#   container_image.digest:  outputs.container.Image\n"
                    "#   e.g. "
                    "sha256:a5ab4ab35b15731c675a531b85ec15c8dd50e36b22d96bcceeca37d016537c8e\n"
                )
            ),
            state="started",
        )

    @operation(outputs={"container": None, "image_path": None})
    def stop(self, **kw: Any) -> Any:
        return unfurl.configurators.ansible.AnsibleConfigurator(
            playbook=Eval(
                {
                    "eval": {
                        "template": "#jinja2: variable_start_string: '<%', "
                        "variable_end_string: '%>'\n"
                        "{% filter from_yaml %}\n"
                        "{%if 'registry_user' | eval %}\n"
                        "- community.docker.docker_login:\n"
                        "     # "
                        "https://docs.ansible.com/ansible/latest/modules/docker_login_module.html#docker-login-module\n"
                        "     # "
                        "https://github.com/ansible/ansible/blob/stable-2.8/lib/ansible/modules/cloud/docker/docker_login.py\n"
                        '     username: "<% SELF.registry_user %>"\n'
                        '     password: "<% SELF.registry_password %>"\n'
                        '     registry_url: "<% SELF.registry_url %>"\n'
                        "{% endif %}\n"
                        "- set_fact:\n"
                        '    image_path: "<% SELF.container_image %>"\n'
                        "- community.docker.docker_container:\n"
                        "    # "
                        "https://docs.ansible.com/ansible/latest/collections/community/docker/docker_container_module.html#ansible-collections-community-docker-docker-container-module\n"
                        "    # "
                        "https://github.com/ansible/ansible/blob/stable-2.8/lib/ansible/modules/cloud/docker/docker_container.py\n"
                        '    name: "<% SELF.name %>" # required\n'
                        '    image: "{{ image_path }}" # Repository path and '
                        "tag\n"
                        '    state: "<%  inputs.state %>"\n'
                        "    {%if SELF.container is defined %}\n"
                        "    {%if SELF.container.pull_policy is defined "
                        "%}                    \n"
                        '    pull: <% SELF.container.pull_policy == "always" '
                        "%>\n"
                        "    {%endif%}\n"
                        "    # filter out env vars set to none\n"
                        "    env: <% SELF.container.environment | map_value | "
                        'dict2items | rejectattr("value", "none") | list | '
                        "items2dict | to_json %>\n"
                        "    # merge container dict after stripping out "
                        '"container_name" and "environment" keys\n'
                        "    <<: <% SELF.container | map_value | dict2items | "
                        'rejectattr("key", "equalto" , "container_name") | '
                        'rejectattr("key", "equalto" , "environment") | '
                        'rejectattr("key", "equalto" , "pull_policy") | list | '
                        "items2dict | to_json %>\n"
                        "    {%endif%}\n"
                        "    <<: <% inputs.configuration | default({}) | "
                        "map_value | to_json %>\n"
                        "    # XXX :\n"
                        "    # api_version: max(set(capabilities.versions) | "
                        "set(host::versions))\n"
                        "{% endfilter %}\n"
                    }
                }
            ),
            resultTemplate=Eval(
                (
                    '{% set status = outputs.container.State.Status | d("") %}\n'
                    '{% set error = outputs.container.State.Error | d("") %}\n'
                    "readyState:\n"
                    "  state: {{ {'created': 'created', 'restarting': 'starting', '': 'initial',\n"
                    "            'running': 'started', 'removing': 'deleting',\n"
                    "            'paused': 'stopped',  'stopped': 'stopped', 'exited': 'deleted', "
                    "'dead': 'deleted'}[status] }}\n"
                    "  local: {%if error %}error\n"
                    "              {% elif status == 'exited' or status == 'dead' %}absent\n"
                    "              {% elif status == 'running' %}ok\n"
                    "              {%else%}pending{%endif%}\n"
                    "# attributes: # XXX \n"
                    "#   container_image.digest:  outputs.container.Image\n"
                    "#   e.g. "
                    "sha256:a5ab4ab35b15731c675a531b85ec15c8dd50e36b22d96bcceeca37d016537c8e\n"
                )
            ),
            state="stopped",
        )

    @operation(outputs={"container": None, "image_path": None})
    def delete(self, **kw: Any) -> Any:
        return unfurl.configurators.ansible.AnsibleConfigurator(
            playbook=Eval(
                {
                    "eval": {
                        "template": "#jinja2: variable_start_string: '<%', "
                        "variable_end_string: '%>'\n"
                        "{% filter from_yaml %}\n"
                        "{%if 'registry_user' | eval %}\n"
                        "- community.docker.docker_login:\n"
                        "     # "
                        "https://docs.ansible.com/ansible/latest/modules/docker_login_module.html#docker-login-module\n"
                        "     # "
                        "https://github.com/ansible/ansible/blob/stable-2.8/lib/ansible/modules/cloud/docker/docker_login.py\n"
                        '     username: "<% SELF.registry_user %>"\n'
                        '     password: "<% SELF.registry_password %>"\n'
                        '     registry_url: "<% SELF.registry_url %>"\n'
                        "{% endif %}\n"
                        "- set_fact:\n"
                        '    image_path: "<% SELF.container_image %>"\n'
                        "- community.docker.docker_container:\n"
                        "    # "
                        "https://docs.ansible.com/ansible/latest/collections/community/docker/docker_container_module.html#ansible-collections-community-docker-docker-container-module\n"
                        "    # "
                        "https://github.com/ansible/ansible/blob/stable-2.8/lib/ansible/modules/cloud/docker/docker_container.py\n"
                        '    name: "<% SELF.name %>" # required\n'
                        '    image: "{{ image_path }}" # Repository path and '
                        "tag\n"
                        '    state: "<%  inputs.state %>"\n'
                        "    {%if SELF.container is defined %}\n"
                        "    {%if SELF.container.pull_policy is defined "
                        "%}                    \n"
                        '    pull: <% SELF.container.pull_policy == "always" '
                        "%>\n"
                        "    {%endif%}\n"
                        "    # filter out env vars set to none\n"
                        "    env: <% SELF.container.environment | map_value | "
                        'dict2items | rejectattr("value", "none") | list | '
                        "items2dict | to_json %>\n"
                        "    # merge container dict after stripping out "
                        '"container_name" and "environment" keys\n'
                        "    <<: <% SELF.container | map_value | dict2items | "
                        'rejectattr("key", "equalto" , "container_name") | '
                        'rejectattr("key", "equalto" , "environment") | '
                        'rejectattr("key", "equalto" , "pull_policy") | list | '
                        "items2dict | to_json %>\n"
                        "    {%endif%}\n"
                        "    <<: <% inputs.configuration | default({}) | "
                        "map_value | to_json %>\n"
                        "    # XXX :\n"
                        "    # api_version: max(set(capabilities.versions) | "
                        "set(host::versions))\n"
                        "{% endfilter %}\n"
                    }
                }
            ),
            resultTemplate=Eval(
                (
                    '{% set status = outputs.container.State.Status | d("") %}\n'
                    '{% set error = outputs.container.State.Error | d("") %}\n'
                    "readyState:\n"
                    "  state: {{ {'created': 'created', 'restarting': 'starting', '': 'initial',\n"
                    "            'running': 'started', 'removing': 'deleting',\n"
                    "            'paused': 'stopped',  'stopped': 'stopped', 'exited': 'deleted', "
                    "'dead': 'deleted'}[status] }}\n"
                    "  local: {%if error %}error\n"
                    "              {% elif status == 'exited' or status == 'dead' %}absent\n"
                    "              {% elif status == 'running' %}ok\n"
                    "              {%else%}pending{%endif%}\n"
                    "# attributes: # XXX \n"
                    "#   container_image.digest:  outputs.container.Image\n"
                    "#   e.g. "
                    "sha256:a5ab4ab35b15731c675a531b85ec15c8dd50e36b22d96bcceeca37d016537c8e\n"
                )
            ),
            state="absent",
        )


__all__ = [
    "unfurl_datatypes_DockerContainer",
    "unfurl_nodes_Container_Application_Docker",
]

