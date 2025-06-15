# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
import io
from unfurl.eval import Ref, map_value
from unfurl.projectpaths import _abspath, _getdir
from unfurl.tosca_plugins.functions import (
    scalar,
    to_dns_label,
    to_googlecloud_label,
    to_kubernetes_label,
    to_label,
)
from unfurl.util import which, wrap_sensitive_value, to_text, to_native
from jinja2 import pass_context
from unfurl.yamlloader import cleartext_yaml
from ansible.errors import AnsibleFilterError


@pass_context
def ref(context, ref, *args, **vars):
    refContext = context["__unfurl"]
    trace = vars.pop("trace", None)
    wantList = vars.pop("wantList", False)
    return Ref(ref, trace=trace, vars=vars).resolve(refContext, wantList=wantList)


@pass_context
def map_value_filter(context, ref, **vars):
    refContext = context["__unfurl"]
    if vars:
        refContext = refContext.copy(vars=vars)
    return map_value(ref, refContext)


@pass_context
def abspath(context, path, relativeTo=None, mkdir=False):
    """
    {{ 'foo' | abspath }}

    or

    {{ 'foo' | abspath('local') }}
    """
    refContext = context["__unfurl"]
    external = _abspath(refContext, path, relativeTo, mkdir)
    refContext.add_external_reference(external)
    return external.get()


@pass_context
def get_dir(context, relativeTo, mkdir=False):
    refContext = context["__unfurl"]
    filepath = _getdir(refContext, relativeTo, mkdir)
    refContext.add_external_reference(filepath)
    return filepath.get()


def to_yaml(a, *args, **kw):
    """Override ansible's built-in filter so we use our own yaml object"""
    default_flow_style = kw.pop("default_flow_style", None)  # XXX
    try:
        output = io.StringIO()
        cleartext_yaml.dump(a, output)
        transformed = output.getvalue()
    except Exception as e:
        raise AnsibleFilterError("to_yaml - %s" % to_native(e), orig_exc=e)
    return to_text(transformed)


# XXX
# def to_nice_yaml(context, a, indent=4, *args, **kw):
#     transformed = yaml.dump(
#         a,
#         Dumper=AnsibleDumper,
#         indent=indent,
#         allow_unicode=True,
#         default_flow_style=False,
#         **kw
#     )
#     return to_text(transformed)

SAFE_FILTERS = {
    "ref": ref,
    "eval": ref,
    "mapValue": map_value_filter,
    "map_value": map_value_filter,
    "sensitive": wrap_sensitive_value,
    "to_label": to_label,
    "to_dns_label": to_dns_label,
    "to_kubernetes_label": to_kubernetes_label,
    "to_googlecloud_label": to_googlecloud_label,
    "scalar": scalar,
    "to_yaml": to_yaml,
}

ALL_FILTERS = dict(
    SAFE_FILTERS,
    **{
        "abspath": abspath,
        "get_dir": get_dir,
        "which": which,
    },
)


class FilterModule:
    def filters(self):
        return ALL_FILTERS
