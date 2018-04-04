import json
import os
import os.path
import sys
import tempfile
import subprocess
from .util import *
from .manifest import *
from . import ansible

"""
* reproducible state of cluster
* doesn't try to re-apply component if component state hasn't changed
* records state of cluster / namespace
* allow assertions

assumption / design principles:
* a cluster is in state X after manifest at commit X was applied successfully
* open world: ommission of component A from manifest doesn't means it isn't installed on cluster
* but by default assumes changes weren't made to declared components
* deterministic: all clusters will have the same state if commit X was applied
* a component retrieved with particular commit id and a set of parameter values
  will deterministically produce the same result when applied to a cluster in a given state
* state X = (component A + component B)
* what about failure states?
* component actions either succeed or fail

* components need to explicitly say how they handle state changes or its an error
** added, removed, updated, config changed
** failure status
** how to proceed? annotate status: ignore, retry, test, install, remove, upgrade; default: fail

using giterop result in
Datebase of recorded configurations/states with record of success, failures, mitigations, learnings

mvp to do
~~~~~~~~~~~~
Goals:
- bring up a new cluster automatically
- update manifest and apply changes to cluster
- changes to cluster (securely) recorded in public repo
- openshift-ansible only
- assume admin rights
- assume only one manifest / repo apply to a cluster

- component error handling / status
- what about deletion?
* explicit deletion/uninstallation
** what if installed isn't in history?
* omission from clusterSpec / component list
  * trigger deletion if previously installed
- what about updates / changes?
* version changes
* parameters change
XXX
* configmap format
  #support semantically equivalent updates:
  = updated-to: component-digest
* git integration?
* params

= Helm notes
release name corresponds to component name (and similarly can't be reused)
tiller stores can store releases as config map (default) or secrets (recommended for security)
helm secret plugin uses sop to enable kms integration
all chart resources need to be in one namespace
chart metadata doesn't map back to source repo, probably should download and check-in chart
but maybe add rules for default repos like https://github.com/kubernetes/charts/tree/master/stable
"""

def save(cluster, manifest):
  #update cluster with last success
  #commit manifest
  pass

PHASES = ('cloud', 'bootstrap', 'user')
def run(manifestPath, opts=None):
  manifest = Manifest(path=manifestPath)
  cluster = Cluster(manifest)
  connections = cluster.connectToCluster(opts)
  #XXX before run commit manifest if it has changed, else verify git access to this manifest
  for phase, i in enumerate(PHASES):
    if cluster.needPhase[phase] or (opts and opts.get('force-' + phase)):
      changes = cluster.syncComponents(phase)
      if changes:
        save(phase, cluster, manifest, changes)
