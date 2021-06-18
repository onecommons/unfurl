# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
from unfurl.eval import Ref, mapValue
from unfurl.projectpaths import _abspath, _getdir
from unfurl.util import which
from jinja2.filters import contextfilter

# from ansible.errors import AnsibleError, AnsibleFilterError


@contextfilter
def ref(context, ref, **vars):
    refContext = context["__unfurl"]
    return Ref(ref, vars=vars).resolveOne(refContext)


@contextfilter
def mapValueFilter(context, ref, **vars):
    refContext = context["__unfurl"]
    if vars:
        refContext = refContext.copy(vars=vars)
    return mapValue(ref, refContext)


@contextfilter
def abspath(context, path, relativeTo=None, mkdir=True):
    """
    {{ 'foo' | abspath }}

    or

    {{ 'foo' | abspath('local') }}
    """
    refContext = context["__unfurl"]
    return _abspath(refContext, path, relativeTo, mkdir).get()


@contextfilter
def get_dir(context, relativeTo, mkdir=True):
    refContext = context["__unfurl"]
    filepath = _getdir(refContext, relativeTo, mkdir)
    return filepath.get()


class FilterModule(object):
    def filters(self):
        return {
            "ref": ref,
            "eval": ref,
            "mapValue": mapValueFilter,
            "abspath": abspath,
            "get_dir": get_dir,
            "which": which,
        }
