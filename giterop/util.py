import six

VERSION = '0.1'
TEMPLATESKEY = 'templates'
CONFIGURATORSKEY = 'configurators'

class GitErOpError(Exception):
  pass

#XXX ansible potential other types: manifests, templates, helmfiles, service broker bundles
ConfiguratorTypes = []
