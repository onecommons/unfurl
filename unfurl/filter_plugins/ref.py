from unfurl.eval import Ref, mapValue

from jinja2.filters import contextfilter

# from ansible.errors import AnsibleError, AnsibleFilterError


@contextfilter
def ref(context, ref):
    refContext = context["__unfurl"]
    return Ref(ref).resolveOne(refContext)


@contextfilter
def mapValueFilter(context, ref):
    refContext = context["__unfurl"]
    return mapValue(ref, refContext)


class FilterModule(object):
    def filters(self):
        return {"ref": ref, "eval": ref, "mapValue": mapValueFilter}
