import unittest
from giterop.runtime import *
from giterop.manifest import *
import traceback
import six
import datetime

class SimpleConfigurator(Configurator):
  def run(self, task):
    assert self.canRun(task)
    yield Status.ok

simpleConfigSpec = ConfigurationSpec('subtask', 'root', 'SimpleConfigurator', 0)

class TestSubtaskConfigurator(Configurator):

  def run(self, task):
    assert self.canRun(task)
    configuration = yield task.createSubTask(simpleConfigSpec)
    assert configuration.status == Status.ok
    yield Status.ok

class ExpandDocTest(unittest.TestCase):
  doc =  {
    't1': {"b": 2},

    "t2": {
      'a': { 'b': 1},
      'c': 'c'
    },

    't3': 'val',

    't4': ['a', 'b'],

    'test1': {
     '+t2': None,
     'a': {'+t1': None },
     'd': {'+t3': None },
     'e': 'e'
    },

    'test2':
      [1, "+t1", '+t4', {'+t4': None}]
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
    expanded = expandDoc(self.doc)
    self.assertEqual(expanded['test1'], self.expected['test1'])
    self.assertEqual(expanded['test2'], self.expected['test2'])

  def test_updateDoc(self):
    # compare and patch: delete keys and items that are in template
    original = {
      'a': 1
    }
    changed = {
      'b': 2
    }
    updated = updateDoc(original, changed)
    self.assertEqual(changed, updated)

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
    assert aggregateError.status == Status.error

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
    simple = {
    'apiVersion': VERSION,
    'root': {
      'resources': {},
      "configurations":{
        "test":{
          "className": "TestSubtaskConfigurator",
          "majorVersion": 0
        }
      }
    }}
    manifest = YamlManifest(simple)
    runner = Runner(manifest)
    output = six.StringIO()
    job = runner.run(JobOptions(add=True, out=output))
    # print('1', output.getvalue())
    assert not job.unexpectedAbort, job.unexpectedAbort.getStackTrace()

    # manifest shouldn't have changed
    manifest2 = YamlManifest(output.getvalue())
    output2 = six.StringIO()
    job2 = Runner(manifest2).run(JobOptions(add=True, out=output2))
    # print('2', output2.getvalue())
    assert not job2.unexpectedAbort, job2.unexpectedAbort.getStackTrace()
    self.assertEqual(output.getvalue(), output2.getvalue())
