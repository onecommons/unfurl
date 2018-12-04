from giterop.eval import Ref

from jinja2.filters import contextfilter
from ansible.errors import AnsibleError, AnsibleFilterError

@contextfilter
def ref(context, ref):
  resource = context['__gitup']
  return Ref(ref).resolveOne(resource)

class FilterModule(object):
  def filters(self):
    return {
      "ref": ref
    }
