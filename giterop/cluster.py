from .util import *

class ConfigMap(object):
  pass

class Cluster(object):
  def __init__(self, manifest, clusterid=None):
    # XXX test if more than one cluster defined in manifest and clusterid not specified
    self.manifest = manifest
    clusterCount = len(self.manifest.clusters)
    if not clusterCount:
      raise GitErOpError("no cluster found in manifest")
    else if clusterCount > 1:
      if not clusterid:
        raise GitErOpError("manifest has more than one cluster, must specify the clusterid")
    elif clusterid:
      cluster = manifest.getCluster(clusterid)
      if not cluster:
        raise GitErOpError("couldn't find cluster %s in manifest" % clusterid)
      self.clusterId = clusterid
      self.clusterSpec = cluster
    else:
      self.clusterSpec = manifest.clusters[0]
      self.clusterId = self.clusterSpec['clusterid'] #XXX test validate checks that clusterid present

  def connectToCluster(self, opts=None):
    '''
    check for hosts, if not found invoke infrastructure playbook in manifest
    check for cluster, if not found invoke control-plane
    '''
    #XXX allow config overrides in opts
    runCloudPhase = not not self.getCloudVars() #XXX
    needCloud = [component in self.clusterDef.components
          if (runCloudPhase or component.phase != 'cloud') and component.connectionType == 'cloud']
    cloudParams = self.getCloudVars() #if cloud section is defined for cluster
    if cloudParams:
      hostsParams = findHostParams(cloudParams)
    elif needCloud:
      raise GitErOpError("cloud config missing")
    # we need cloud vars and cloud credentials
    #if there are control pane components then need hostnames and ssh connection info
    runBootstrapPhase = True
    needToConnectToHosts = [component in self.clusterSpec.components
      if (runBootstrapPhase or component.phase != 'bootstrap') and component.connectionType == 'hosts']
    if needToConnectToHosts and not hostsParams:
      hostsParams = self.getHostVars()
    elif hostsParams and self.getHostVars():
      hostsParams = self.updateAndValidateHostParams(hostsParams)
    clientContext = self.getClientContext(hostsParams)
    needKubeManifest = [component in self.clusterDef.components if component.connectionType == 'kubectrl']
    if needKubeManifest and not clientContext:
      #if neither we just need client host connection info (default: localhost), kube.manifest
      # and binaries path (kubectrl, oc, helm)
      # what if we want to run inside a container like landscaper or an ansible service broker bundle
      # can use oc and kubectrl connection plugins available in ansible 2.5
      clientContext  = getClientContextOnHost(hostsParams)
    self.connections = (cloudParams, hostsParams, clientContext)
    return self.connections

  def getConfigMap(self):
    result = _runPlaybook(localAnsible('get-configmap.yaml'), self.getParams())
    return parse_result(result).configmap

  def updateConfigMap(self, foo):
    _runPlaybook(localAnsible('update-configmap.yaml'), self.getParams({'foo':foo}))

  def getParams(merge):
    '''
    well-known parameters:
cluster_id
kube.config, kubectl_binary
AWS_SECRET_ACCESS_KEY, AWS_ACCESS_KEY_ID, AWS_REGION
    '''
    return {}

  def applyComponentChange(self, installed, manifestComponent):
    '''
    run playbook, update configmap with status
    '''

  def installComponent(self, component):
    '''
    invoke component playbook
    '''

  def removeComponent(self, component):
    '''
    '''

  def syncComponents(self, phase):
    pass

  def compareUserPhaseComponents(self, phase):
    '''
    if no configmap, create empty one and apply each component
    else compare each component:
      if not in manifest, annotate component status as orphaned as of this changeset,
      if updated, annotate component status as out-of-date as of this changeset,
      if not in configmap, install component
      if in both, compare components and parameters, if different re-apply component
    '''
    cluster = self
    manifest = self.manifest
    configMap = cluster.getStatus(phase)
    changed = False
    if not configMap:
      cluster.createConfigMap()
      for component in manifest.installed():
        if cluster.installComponent(component):
          changed = True
    else:
      for installed in configMap:
        manifestComponent = manifest.installed.get(installed)
        if not manifestComponent:
          cluster.removeComponent(installed)
          changed = True
        else:
          cluster.applyComponentChange(installed, manifestComponent)
      for manifestComponent in manifest.installed:
        if manifestComponent not in configMap:
          cluster.installComponent(manifestComponent)
          changed = True
    return changed
