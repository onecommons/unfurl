# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
import re

from jinja2.filters import contextfilter

from unfurl.eval import Ref, mapValue
from unfurl.support import abspath, getdir
from unfurl.util import which

NUMBERS = re.compile(r"[^\d.]")


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


def only_numbers(string: str) -> str:
    """Removes everything from the string except for digits and `.`

    Example: " 200.2 GB" -> "200.2"
    """
    result = re.sub(NUMBERS, "", string)
    if result:
        return result
    raise ValueError(f"No numbers found in '{string}'")


class FilterModule(object):
    def filters(self):
        return {
            "ref": ref,
            "eval": ref,
            "mapValue": mapValueFilter,
            "abspath": _abspath,
            "get_dir": get_dir,
            "which": which,
            "only_numbers": only_numbers,
        }
