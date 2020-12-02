# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
import os.path
from unfurl.eval import Ref, mapValue
from unfurl.support import abspath, getdir
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
def _abspath(context, path, relativeTo=None, mkdir=True):
    """
    {{ 'foo' | abspath }}

    or

    {{ 'foo' | abspath('local') }}
    """
    refContext = context["__unfurl"]
    return abspath(refContext, path, relativeTo, mkdir)


@contextfilter
def get_dir(context, relativeTo, mkdir=True):
    refContext = context["__unfurl"]
    return getdir(refContext, relativeTo, mkdir)


class FilterModule(object):
    def filters(self):
        return {
            "ref": ref,
            "eval": ref,
            "mapValue": mapValueFilter,
            "abspath": _abspath,
            "get_dir": get_dir,
            "which": which,
        }
