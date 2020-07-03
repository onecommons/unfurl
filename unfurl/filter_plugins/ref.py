from unfurl.eval import Ref, mapValue

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


class FilterModule(object):
    def filters(self):
        return {"ref": ref, "eval": ref, "mapValue": mapValueFilter}
