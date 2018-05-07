import unittest
from giterop import *
from giterop.util import *
from giterop.resource import *
from giterop.manifest import *
from ruamel.yaml.comments import CommentedMap

class AttributeTest(unittest.TestCase):

  @unittest.expectedFailure
  def test_CommentedMap(self):
    cm = CommentedMap()
    # bug in ruamel.yaml: raises TypeError: source has undefined order
    assertEqual(cm, cm.copy())

  def test_metadata(self):
    resourceDef = { "metadata": {
      "name": "test",
      'a': {'valueFrom': ':name'}
      }
    }
    manifest = Manifest({"apiVersion": VERSION})
    resource = ResourceDefinition(manifest, resourceDef).resource
    self.assertEqual(resource['a'], 'test')
    resource['newkey'] = {'valueFrom': '.'}
    assert resource['newkey'] is resource
