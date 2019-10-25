from unfurl.eval import Ref

from jinja2.filters import contextfilter
from ansible.errors import AnsibleError, AnsibleFilterError

@contextfilter
def ref(context, ref):
  refContext = context['__unfurl']
  return Ref(ref).resolveOne(refContext)

class FilterModule(object):
  def filters(self):
    return {
      "ref": ref
    }
