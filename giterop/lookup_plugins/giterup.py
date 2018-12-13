from giterop.eval import Ref
# from ansible.errors import AnsibleError

#module name is lookup name
from ansible.plugins.lookup import LookupBase
class LookupModule(LookupBase):
  def run(self, terms, variables, **kwargs):
    # resource should be current host or current config if no host
    resource = variables['__giterop']
    return list(map(lambda term: Ref(term).resolveOne(resource), terms))
