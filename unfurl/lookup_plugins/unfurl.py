# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
import sys

# from unfurl.eval import Ref # broken in 2.7

# from ansible.errors import AnsibleError

# module name is lookup name
from ansible.plugins.lookup import LookupBase


class LookupModule(LookupBase):
    def run(self, terms, variables, **kwargs):
        # resource should be current host or current config if no host
        refContext = variables["__unfurl"]
        # workaround for 2.7
        Ref = sys.modules[type(variables["__unfurl"]).__module__].Ref

        return list(map(lambda term: Ref(term).resolveOne(refContext), terms))
