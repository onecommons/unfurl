from unfurl.eval import Ref, mapValue

from jinja2.filters import contextfilter

# from ansible.errors import AnsibleError, AnsibleFilterError
import os.path


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
def abspath(context, path):
    refContext = context["__unfurl"]
    # note: refContext.baseDir will be the baseDir of the source file this appears in
    # but refContext.currentResource.baseDir will be the baseDir of the current instance, usually the ensemble's basedir
    return os.path.abspath(os.path.join(refContext.currentResource.baseDir, path))


class FilterModule(object):
    def filters(self):
        return {"ref": ref, "eval": ref, "mapValue": mapValueFilter, "abspath": abspath}
