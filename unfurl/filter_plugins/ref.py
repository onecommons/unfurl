# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
from unfurl.eval import Ref, map_value
from unfurl.projectpaths import _abspath, _getdir
from unfurl.util import which
from jinja2.filters import contextfilter

# from ansible.errors import AnsibleError, AnsibleFilterError


@contextfilter
def ref(context, ref, **vars):
    refContext = context["__unfurl"]
    return Ref(ref, vars=vars).resolve_one(refContext)


@contextfilter
def map_value_filter(context, ref, **vars):
    refContext = context["__unfurl"]
    if vars:
        refContext = refContext.copy(vars=vars)
    return map_value(ref, refContext)


@contextfilter
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


@contextfilter
def get_dir(context, relativeTo, mkdir=False):
    refContext = context["__unfurl"]
    filepath = _getdir(refContext, relativeTo, mkdir)
    refContext.add_external_reference(filepath)
    return filepath.get()


class FilterModule:
    def filters(self):
        return {
            "ref": ref,
            "eval": ref,
            "mapValue": map_value_filter,
            "abspath": abspath,
            "get_dir": get_dir,
            "which": which,
        }
