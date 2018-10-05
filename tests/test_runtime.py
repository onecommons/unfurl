import unittest
from giterop.runtime import *
import traceback
import six
import datetime

class SimpleConfigurator(Configurator):
  def run(self, task):
    assert self.canRun(task)
    yield Status.ok

simpleConfigSpec = ConfigurationSpec('subtask', 'root',  ConfiguratorSpec('test', 'SimpleConfigurator', 0), 0)

class TestSubtaskConfigurator(Configurator):

  def run(self, task):
    assert self.canRun(task)
    configuration = yield task.createSubTask(simpleConfigSpec)
    assert configuration.status == Status.ok
    yield Status.ok

class JobTest(unittest.TestCase):

  def test_lookupClass(self):
    assert lookupClass("SimpleConfigurator") is SimpleConfigurator

  def test_runner(self):
    rootResource = Resource('root')
    configuratorSpec = ConfiguratorSpec('test', 'TestSubtaskConfigurator', 0)
    configurationSpec = ConfigurationSpec('test', 'root', configuratorSpec, 0)
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
