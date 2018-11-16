import unittest
from giterop.runtime import JobOptions, Configurator, ConfigurationSpec, Status, Priority, Resource, Runner, Manifest, OperationalInstance
from giterop.manifest import YamlManifest
from giterop.util import GitErOpError, expandDoc, restoreIncludes, lookupClass, VERSION, diffDicts, mergeDicts, patchDict
from ruamel.yaml.comments import CommentedMap
import traceback
import six
import datetime
import copy

class SimpleConfigurator(Configurator):
  def run(self, task):
    assert self.canRun(task)
    yield task.createResult(True, True, Status.ok)

simpleConfigSpec = ConfigurationSpec('subtask', 'root', 'SimpleConfigurator', 0)

class TestSubtaskConfigurator(Configurator):

  def run(self, task):
    assert self.canRun(task)
    configuration = yield task.createSubTask(simpleConfigSpec)
    assert configuration.status == Status.ok
    # print ("running TestSubtaskConfigurator")
    yield task.createResult(True, True, Status.ok)

class ExpandDocTest(unittest.TestCase):
  doc =  {
    't1': {"b": 2},

    "t2": {
      'a': { 'b': 1},
      'c': 'c'
    },

    't3': 'val',

    't4': ['a', 'b'],

    'test1': CommentedMap([
     ('+t2', None),
     ('a', {'+t1': None }),
     ('d', {'+t3': None }),
     ('e', 'e')
    ]),

    'test2':
      [1, "+t1", '+t4', {'+t4': None}],
  }

  expected = {
    'test1': {
      'a': { 'b': 2},
      'c': 'c',
      'd': 'val',
      'e': 'e'
    },
    'test2':
      [1, {"b": 2}, 'a', 'b', 'a', 'b']
  }

  def test_expandDoc(self):
    includes, expanded = expandDoc(self.doc, cls=CommentedMap)
    self.assertEqual(includes, {
      ('test1',): [('+t2', None)],
      ('test1', 'a'): [('+t1', None)],
      ('test1', 'd'): [('+t3', None)],
      ('test2', 1): [('+t1', None)],
      ('test2', 2): [('+t4', None)],
      ('test2', 3): [('+t4', None)],
    })
    self.assertEqual(expanded['test1'], self.expected['test1'])
    self.assertEqual(expanded['test2'], self.expected['test2'])
    restoreIncludes(includes, self.doc, expanded, CommentedMap)
    self.assertEqual(expanded['test1'], self.doc['test1'])

  def test_diff(self):
    expectedOld = {'a': 1, 'b': {'b1': 1, 'b2': 1}, 'd':1}
    old = copy.copy(expectedOld)
    new = {'a': 1, 'b': {'b1': 2, 'b2': 1}, 'c': 2}
    diff = diffDicts(old, new)
    self.assertEqual(diff, {'b': {'b1': 2}, 'c': 2, 'd': {'+%': 'delete'}})
    newNew = mergeDicts(old, diff)
    self.assertEqual(newNew, new)
    self.assertEqual(old, expectedOld)
    patchDict(old, new)
    self.assertEqual(old, new)

  def test_recursion(self):
    doc = {
      "test3": {
        "a": {
          "recurse": {"+test3": None}
        }
      },
    }
    with self.assertRaises(GitErOpError) as err:
      includes, expanded = expandDoc(doc)
    self.assertEqual(str(err.exception),
      '''recursive include "['test3']" in "('test3', 'a', 'recurse')"''')

    # XXX fix ../
    # doc2 = {
    #   "test4": {
    #     "+../test4": None,
    #     "child": {}
    #   }
    # }
    # includes, expanded = expandDoc(doc2)

class JobTest(unittest.TestCase):

  def test_lookupClass(self):
    assert lookupClass("SimpleConfigurator") is SimpleConfigurator

  def test_runner(self):
    rootResource = Resource('root')
    configurationSpec = ConfigurationSpec('test', 'root', 'TestSubtaskConfigurator', 0)
    specs = [configurationSpec]
    runner = Runner(Manifest(rootResource, specs))
    job = runner.run(JobOptions(add=True))
    assert not job.unexpectedAbort, job.unexpectedAbort.stackInfo

class OperationalInstanceTest(unittest.TestCase):

  def test_aggregate(self):
    ignoredError = OperationalInstance("error", "ignore")
    assert ignoredError.status == Status.error
    assert ignoredError.priority == Priority.ignore
    assert not ignoredError.operational
    assert not ignoredError.required

    requiredError = OperationalInstance("error", "required")
    assert requiredError.status == Status.error
    assert requiredError.priority == Priority.required
    assert requiredError.required
    assert not requiredError.operational

    aggregateOK = OperationalInstance(Status.ok)
    aggregateOK.dependencies = [ignoredError]
    assert aggregateOK.status == Status.ok

    aggregateError = OperationalInstance(Status.ok)
    aggregateError.dependencies = [ignoredError, requiredError]
    self.assertEqual(aggregateError.status, Status.error)

    aggregateError = OperationalInstance(Status.notapplied)
    aggregateError.dependencies = [ignoredError, requiredError]
    self.assertEqual(aggregateError.status, Status.error)

    aggregateError = OperationalInstance(Status.notapplied)
    aggregateError.dependencies = [OperationalInstance("ok", "optional")]
    self.assertEqual(aggregateError.status, Status.ok)

    aggregateError = OperationalInstance(Status.notapplied)
    aggregateError.dependencies = []
    self.assertEqual(aggregateError.status, Status.notapplied)

class RunTest(unittest.TestCase):
  """
  XXX:
  test status: report last state, report config changes, report plan
  test read-ony/discover
  test failing configurations with abort, continue, revert (both capable of reverting and not)
  test incomplete runs
  test unexpected errors, terminations
  test locking
  test required metadata on resources
  """
  # XXX3 test case: a failed configuration with intent=revert should stay in configurations as error
  # XXX3 test: hide configurations that are both notpresent and revert / skip

  def test_manifest(self):
    simple = """
    apiVersion: %s
    kind: Manifest
    root:
      resources: {}
      spec:
        configurations:
          test:
            className:    TestSubtaskConfigurator
            majorVersion: 0
            parameters: {}
    """ % VERSION
    manifest = YamlManifest(simple)
    runner = Runner(manifest)
    self.assertEqual(runner.lastChangeId, 0)
    output = six.StringIO()
    job = runner.run(JobOptions(add=True, out=output, startTime="test"))
    assert not job.unexpectedAbort, job.unexpectedAbort.getStackTrace()
    # workDone includes subtasks
    assert len(job.workDone) == 2, job.workDone

    # manifest shouldn't have changed
    manifest2 = YamlManifest(output.getvalue())
    self.assertEqual(manifest2.lastChangeId, 3)
    output2 = six.StringIO()
    job2 = Runner(manifest2).run(JobOptions(add=True, out=output2, startTime="test"))
    #  print('2', output2.getvalue())
    assert not job2.unexpectedAbort, job2.unexpectedAbort.getStackTrace()
    # should not find any tasks to run
    assert len(job2.workDone) == 0, job2.workDone
    self.maxDiff = None
    self.assertEqual(output.getvalue(), output2.getvalue())

  def test_template_inheritance(self):
    manifest = '''
apiVersion: giterops/v1alpha1
kind: Manifest
configurators:
  step1:
    spec:
      className: "TestSubtaskConfigurator"
      majorVersion: 0
templates:
  base:
    configurations:
      step1:
        +configurators/step1:
root:
  resources:
    cloud3: #key is resource name
      +templates/base:
      +templates/production:
'''
    with self.assertRaises(GitErOpError) as err:
      YamlManifest(manifest)
    self.assertEqual(str(err.exception), 'can not find "templates/production" in document')
