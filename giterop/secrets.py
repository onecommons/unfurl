"""
Attributes and parameters can be marked as secret in their definition
which will cause their values to automatically be saved and retrieved from the kms
instead of being stored in the manifest.

Attributes can be declared in a template definition
independent of the rest of the declaration,
enabling the user to protect secrets without requiring cooperation of
the configurator.

A reference to a secret can be made like any other value reference
and a secret store is represented like any other resource, except it's "kind"
is associated implementation that knows how to marshall and unmarshall the resource's
attributes in the key store.
"""

class KMSResource(Resource):
  """
  Represents a Key Management System resource used for storing secrets

  It's attributes are stored in the kms, not the manifest
  Secrets can be stored and retrieved using valuerefs to this resource.
  """
  def makeMetadata(self):
    self.kms = self.bind(self.definition)
    return KMSMetadataDict(self.defintion, self.kms)

  def bind(self): #XXX
    """
    connect to the kms service that this resource represents
    """
    return None

registerClass(VERSION, "KMSResource", KMSResource)

class KMSMetadataDict(MetadataDict):
  def __init__(self, resourceDef, kms):
    super(MetadataDict, self).__init__(resourceDef)
    self.kms = kms

  def __getitem__(self, name):
    #XXX needs to call super somehow?
    return self.kms.get(name)

  def __setattr__(self, name, value):
    #XXX needs to call super somehow?
    return self.kms.set(name, value)
