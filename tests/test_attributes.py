import unittest
from giterop import *
from giterop.util import *
from giterop.resource import *
from giterop.run import *
from ruamel.yaml.comments import CommentedMap

class AttributeTest(unittest.TestCase):
  longMessage = True

  def test_CommentedMap(self):
    cm = CommentedMap()
    # check bug in ruamel.yaml is fixed: raises TypeError: source has undefined order
    self.assertEqual(cm, cm.copy())

  def test_CommentedMapEquality(self):
    cm = CommentedMap((('b', 2),))
    cm.insert(1, 'a', 1, comment="a comment")
    self.assertEqual(cm, {'a':1, 'b': 2})

  def _getTestResource(self):
    resourceDef = { "metadata": {
      "name": "test",
      'a': {'ref': ':name'},
      'b': [1, 2, 3],
      'd': {'a':'va', 'b':'vb'},
      'n': {'n':{'n':'n'}},
      's': {'ref': '.'},
      'x': [{
              'a': [{'c':1}, {'c':2}, {'b': 'exists'}, {'l': ['l1']}, {'l': ['l2']}],
            },
            [{'c':5}],
            {
              "a": [{'c':3}, {'c':4}, {'l': ['l3']}, {'l': ['l4']}]
            },
            [{'c':6}],
          ],
      }
    }
    manifest = Manifest({"apiVersion": VERSION})
    resource = ResourceDefinition(manifest, resourceDef).resource
    return resource

  def test_metadata(self):
    resource = self._getTestResource()
    self.assertEqual(resource['a'], 'test')
    resource['newkey'] = {'ref': '.'}
    assert resource['newkey'] is resource, resource['newkey']
    varref = {
      'ref': 'd:$key',
      'vars': {
        'key': 'a'
      }
    }
    self.assertEqual(Ref(varref).resolve(resource), ["va"], varref)
    varref['vars']['key'] = 'missing'
    self.assertEqual(Ref(varref).resolve(resource), [], varref)

  def test_refs(self):
    assert Ref.isRef({'ref': ':name'})
    assert not Ref.isRef({'ref': ':name', 'somethingUnexpected': 1})
    assert Ref.isRef({
      'ref': ':name',
      'vars': {
        'a': None
      }
    })

  def test_refPaths(self):
    resource = self._getTestResource()
    for (exp, expected) in [
      ['x?:a[c=4]', [[{'c':3}, {'c':4}, {'l': ['l3']}, {'l': ['l4']}]] ],
      ['x:a[c]?', [[{'c':1}, {'c':2}, {'b': 'exists'}, {'l': ['l1']}, {'l': ['l2']}]]],
      ['x:a:[c]', [{'c':1}, {'c':2}, {'c':3}, {'c':4}]],
      ['x:a?:[c]', [{'c':1}, {'c':2}]],
      ['a', ['test']],
      ['b', [[1,2,3]]],
      ['?:b:2', [3]],
      ['[b:2]:0:b:2', [3]],
      ['b:1', [2]],
      ['s:b', [[1, 2, 3]]],
      ['s:b:1', [2]],
      ['s:s:b:1', [2]],
      ['n:n:n', ['n']],
      ['d[a=va]', [{'a':'va', 'b':'vb'}]],
      ['d[a=vb]', []],
      ['b[1=2]', [[1,2,3]]],
      ['b[1=1]', []],
      ['a[=test]', ['test']],
      ['a[!=test]', []],
      ['a[key]', []],
      ['d[a=va][b=vb]', [{'a':'va', 'b':'vb'}]],
      ['d[a=va][a=vb]', []],
      ['d[a=va][a!=vb]', [{'a':'va', 'b':'vb'}]],
      ['d[a=va]:b', ['vb']],
      ['x:a:c', [1,2,3,4]],
      ['x:c', [5, 6]],
      ['x:[c]', [{'c':5}, {'c':6}]],
      ['x:a[b]:c', [1,2]],
      ['x:a[!b]:c', [3,4]],
      ['x:a:l', [['l1'],['l2'],['l3'],['l4']]],
      [{'ref': 'a[=$yes]',
        'vars': {'yes': 'test'}
       },  ['test']],
      [{'ref': 'a[=$no]',
        'vars': {'no': None}
       },  []],
       ['[a]', [resource]],
       ['[=blah]', []],
       ['[blah]', []],
       ['[!blah]', [resource]],
       ['.[!=blah]', [resource]],
       ['[!a]', []],
       ['.named:test', [resource]],
       #XXX test nested ['.[k[d=3]=4]']
    ]:
      ref = Ref(exp)
      #print ('eval', ref, ref.paths)
      self.assertEqual(ref.resolve(resource), expected, [ref] + ref.paths)

  def test_funcs(self):
    resource = self._getTestResource()
    test1 = {
      'eval': '.name',
      'vars': {
        'a': None
      }
    }
    test2 = {
      'eval': '$b',
      'vars': {
        'b': 1
      }
    }
    test3 = {
      'eval': {
        'if': {'not': '$a'},
        'then': {'q': 'expected'},
        'else': {'q': 'unexpected'},
      },
      'vars': {
        'a': None
      }
    }
    result1 = evalDict(test1, resource)
    self.assertEqual('test', result1)
    result2 = evalDict(test2, resource)
    self.assertEqual(1, result2)
    result3 = evalDict(test3, resource)
    self.assertEqual('expected', result3)
