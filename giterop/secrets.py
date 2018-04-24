"""
Secrets are automatically marshalled and unmarshalled
by declaring an attribute a secret in its definition.

Attributes can be declared in a template definition
independent of the rest of the declaration,
enabling the user to protect secrets without requiring cooperation of
the configurator.

A reference to a secret can be made like any other value reference
and a secret store is represented like any other resource, except it's "kind"
is associated implementation that knows how to marshall and unmarshall the resource's
attributes in the key store.
"""

class KMSAttributeMarshaller(AttributeMarshaller):
  """
  All attributes are stored in the kms, not the metadata
  """
  def __init__(self, resource):
    self.resource = resource
    self.kms = self.bind(resource)

  def __getattr__(self, name, default=None):
    return self.kms.get(name, default)

  def __setattr__(self, name, value):
    return self.kms.set(name, value)
