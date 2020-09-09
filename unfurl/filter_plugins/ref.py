import os.path
from unfurl.eval import Ref, mapValue
from unfurl.support import abspath as _abspath

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
    return _abspath(refContext, path, relativeTo, mkdir)


class FilterModule(object):
    def filters(self):
        return {"ref": ref, "eval": ref, "mapValue": mapValueFilter, "abspath": abspath}
