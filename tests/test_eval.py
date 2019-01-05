import unittest
from giterop.eval import Ref, mapValue, serializeValue, runTemplate, RefContext
from giterop.runtime import Resource
from ruamel.yaml.comments import CommentedMap

class EvalTest(unittest.TestCase):
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
    resourceDef = {
      "name": "test",
      'a': {'ref': 'name'},
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
      'e': {'a1':{'b1': 'v1'},
            'a2':{'b2': 'v2'}},
      'f': {'a': 1, 'b': {'ref': '.::f::a'} },
      }
    resource = Resource("test", attributes=resourceDef)
    return resource

  def test_refs(self):
    assert Ref.isRef({'ref': '::name'})
    assert not Ref.isRef({'ref': '::name', 'somethingUnexpected': 1})
    assert Ref.isRef({
      'ref': '::name',
      'vars': {
        'a': None
      }
    })

  def test_refPaths(self):
    resource = self._getTestResource()
    for (exp, expected) in [
      ['x?::a[c=4]', [[{'c':3}, {'c':4}, {'l': ['l3']}, {'l': ['l4']}]] ],
      ['x::a[c]?', [[{'c':1}, {'c':2}, {'b': 'exists'}, {'l': ['l1']}, {'l': ['l2']}]]],
      ['x::a::[c]', [{'c':1}, {'c':2}, {'c':3}, {'c':4}]],
      ['x::a?::[c]', [{'c':1}, {'c':2}]],
      ['a', ['test']],
      ['b', [[1,2,3]]],
      ['b?::2', [3]],
      ['[b::2]::0::b::2', [3]],
      ['b::1', [2]],
      ['s::b', [[1, 2, 3]]],
      ['s::b::1', [2]],
      ['s::s::b::1', [2]],
      ['n::n::n', ['n']],
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
      ['d[a=va]::b', ['vb']],
      ['x::a::c', [1,2,3,4]],
      ['x::c', [5, 6]],
      ['x::[c]', [{'c':5}, {'c':6}]],
      ['x::a[b]::c', [1,2]],
      ['x::a[!b]::c', [3,4]],
      ['x::a::l', [['l1'],['l2'],['l3'],['l4']]],
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
       ['::test', [resource]],
       ['d::*', set(['va', 'vb'])],
       ['e::*::b2', ['v2']],
       ["*", []],
       ["f", [{'a': 1, 'b': 1}]],
       #XXX test nested ['.[k[d=3]=4]']
    ]:
      ref = Ref(exp)
      # print ('eval', ref, ref.source)
      if isinstance(expected, set):
        # for results where order isn't guaranteed in python2.7
        self.assertEqual(set(ref.resolve(RefContext(resource))), expected, ref.source)
      else:
        self.assertEqual(ref.resolve(RefContext(resource)), expected, ref.source)

  def test_funcs(self):
    resource = self._getTestResource()
    test1 = {
      'ref': '.name',
      'vars': {
        'a': None
      }
    }
    test2 = {
      'ref': '$b',
      'vars': {
        'b': 1
      }
    }
    test3 = {
      'ref': {
        'if': {'not': '$a'},
        'then': {'q': 'expected'},
        'else': {'q': 'unexpected'},
      },
      'vars': {
        'a': None
      }
    }
    result1 = Ref(test1).resolveOne(RefContext(resource))
    self.assertEqual('test', result1)
    result2 = Ref.resolveOneIfRef(test2, resource)
    self.assertEqual(1, result2)
    result3 = Ref.resolveOneIfRef(test3, resource)
    self.assertEqual('expected', result3)
    result4 = Ref.resolveIfRef(test3, resource)
    self.assertEqual(['expected'], result4)

  def test_forEach(self):
    resource = self._getTestResource()
    test1 = {
      'ref': '.',
      'foreach': {
          'key':   '.name',
          'value': {'content': { 'ref': 'b'}}
      },
    }
    expected = {
      'test': {'content': [1, 2, 3]}
    }
    result1 = Ref(test1).resolve(RefContext(resource))
    self.assertEqual([expected], result1)
    result2 = Ref(test1).resolveOne(RefContext(resource))
    self.assertEqual(expected, result2)

  def test_serializeValues(self):
    resource = self._getTestResource()
    src = {'a': ['b', resource]}
    serialized = serializeValue(src)
    self.assertEqual(serialized, {'a': ['b', {'ref': '::test'}]})
    self.assertEqual(src, mapValue(serialized, resource))

  def test_template(self):
    self.assertEqual(runTemplate(" {{ foo }} ", {"foo": "hello"}), " hello ")
    from giterop.runtime import Resource
    vars = dict(__giterop = RefContext(Resource("test", attributes=dict(a1="hello"))))
    self.assertEqual(runTemplate(' {{ "::test::a1" | ref }} ', vars), u" hello ")
    self.assertEqual(runTemplate(' {{ lookup("giterup", "::test::a1") }} ', vars), u" hello ")
    self.assertEqual(runTemplate('{{  query("giterup", "::test::a1") }}', vars), [u'hello'])
