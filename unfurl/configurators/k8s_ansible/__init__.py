# contents in this file are derived from
# https://github.com/ansible-collections/kubernetes.core/blob/stable-2.1/plugins/module_utils/common.py

# Copyright 2018 Red Hat | Ansible
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import base64
import os
import hashlib

import kubernetes

from .args_common import (AUTH_ARG_MAP, AUTH_ARG_SPEC, AUTH_PROXY_HEADERS_SPEC)

from ansible.module_utils.six import iteritems
from ansible.module_utils._text import to_native, to_bytes, to_text

from .k8sdynamicclient import K8SDynamicClient
from .client.discovery import LazyDiscoverer

try:
    import urllib3
    urllib3.disable_warnings()
except ImportError:
    pass


def configuration_digest(configuration):
    m = hashlib.sha256()
    for k in AUTH_ARG_MAP:
        if not hasattr(configuration, k):
            v = None
        else:
            v = getattr(configuration, k)
        if v and k in ["ssl_ca_cert", "cert_file", "key_file"]:
            with open(str(v), "r") as fd:
                content = fd.read()
                m.update(content.encode())
        else:
            m.update(str(v).encode())
    digest = m.hexdigest()
    return digest


def get_api_client(module=None, **kwargs):
    auth = {}

    def _raise_or_fail(exc, msg):
        if module:
            module.fail_json(msg % to_native(exc))
        raise exc

    # If authorization variables aren't defined, look for them in environment variables
    for true_name, arg_name in AUTH_ARG_MAP.items():
        if module and module.params.get(arg_name) is not None:
            auth[true_name] = module.params.get(arg_name)
        elif arg_name in kwargs and kwargs.get(arg_name) is not None:
            auth[true_name] = kwargs.get(arg_name)
        elif arg_name == "proxy_headers":
            # specific case for 'proxy_headers' which is a dictionary
            proxy_headers = {}
            for key in AUTH_PROXY_HEADERS_SPEC.keys():
                env_value = os.getenv('K8S_AUTH_PROXY_HEADERS_{0}'.format(key.upper()), None)
                if env_value is not None:
                    if AUTH_PROXY_HEADERS_SPEC[key].get('type') == 'bool':
                        env_value = env_value.lower() not in ['0', 'false', 'no']
                    proxy_headers[key] = env_value
            if proxy_headers is not {}:
                auth[true_name] = proxy_headers
        else:
            env_value = os.getenv('K8S_AUTH_{0}'.format(arg_name.upper()), None) or os.getenv('K8S_AUTH_{0}'.format(true_name.upper()), None)
            if env_value is not None:
                if AUTH_ARG_SPEC[arg_name].get('type') == 'bool':
                    env_value = env_value.lower() not in ['0', 'false', 'no']
                auth[true_name] = env_value

    def auth_set(*names):
        return all([auth.get(name) for name in names])

    if auth_set('host'):
        # Removing trailing slashes if any from hostname
        auth['host'] = auth.get('host').rstrip('/')

    if auth_set('username', 'password', 'host') or auth_set('api_key', 'host'):
        # We have enough in the parameters to authenticate, no need to load incluster or kubeconfig
        pass
    elif auth_set('kubeconfig') or auth_set('context'):
        try:
            kubernetes.config.load_kube_config(auth.get('kubeconfig'), auth.get('context'), persist_config=auth.get('persist_config'))
        except Exception as err:
            _raise_or_fail(err, 'Failed to load kubeconfig due to %s')

    else:
        # First try to do incluster config, then kubeconfig
        try:
            kubernetes.config.load_incluster_config()
        except kubernetes.config.ConfigException:
            try:
                kubernetes.config.load_kube_config(auth.get('kubeconfig'), auth.get('context'), persist_config=auth.get('persist_config'))
            except Exception as err:
                _raise_or_fail(err, 'Failed to load kubeconfig due to %s')

    # Override any values in the default configuration with Ansible parameters
    # As of kubernetes-client v12.0.0, get_default_copy() is required here
    try:
        configuration = kubernetes.client.Configuration().get_default_copy()
    except AttributeError:
        configuration = kubernetes.client.Configuration()

    for key, value in iteritems(auth):
        if key in AUTH_ARG_MAP.keys() and value is not None:
            if key == 'api_key':
                setattr(configuration, key, {'authorization': "Bearer {0}".format(value)})
            elif key == 'proxy_headers':
                headers = urllib3.util.make_headers(**value)
                setattr(configuration, key, headers)
            else:
                setattr(configuration, key, value)

    digest = configuration_digest(configuration)
    if digest in get_api_client._pool:
        client = get_api_client._pool[digest]
        return client

    try:
        client = K8SDynamicClient(kubernetes.client.ApiClient(configuration), discoverer=LazyDiscoverer)
    except Exception as err:
        _raise_or_fail(err, 'Failed to get client due to %s')

    get_api_client._pool[digest] = client
    return client


get_api_client._pool = {}
