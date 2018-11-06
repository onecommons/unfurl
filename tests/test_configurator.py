import unittest
from giterop.manifest import YamlManifest
from giterop.runtime import Runner, Configurator, JobOptions, Status
import traceback
import six
import datetime

class TestConfigurator(Configurator):
  def run(self, task):
    assert self.canRun(task)
    attributes = task.currentConfiguration.resource.attributes
    attributes['copyOfMeetsTheRequirement'] = attributes["meetsTheRequirement"]
    yield Status.ok

manifest = '''
apiVersion: giterops/v1alpha1
kind: Manifest
configurations:
  test:
    apiVersion: giterops/v1alpha1
    className: TestConfigurator
    majorVersion: 0
    requires:
      properties:
        meetsTheRequirement:
          type: string
      required: ['meetsTheRequirement']
    provides:
      properties:
        copyOfMeetsTheRequirement:
          enum: ["copy"]
      required: ['copyOfMeetsTheRequirement']
    parameters: {}
    parameterSchema: {}
root:
  resources:
    test1:
      spec:
        attributes:
          meetsTheRequirement: "copy"
        configurations:
          +configurations:
    test2:
      spec:
        attributes:
          meetsTheRequirement: false
        configurations:
          +configurations:
'''

class ConfiguratorTest(unittest.TestCase):
  def test_neededTasks(self):
    """
    test that runner figures out the proper tasks to run
    """
    runner = Runner(YamlManifest(manifest))
    test1 = runner.manifest.getRootResource().findResource('test1')
    missing = runner.manifest.specs[0].findMissingRequirements(test1)
    assert not missing, missing

    run1 = runner.run(JobOptions(resource='test1'))
    assert len(run1.workDone) == 1, run1.workDone
    assert not run1.unexpectedAbort, run1.unexpectedAbort.getStackTrace()

  def test_requires(self):
    """test that the configuration only runs if the resource meets the requirements"""
    runner = Runner(YamlManifest(manifest))
    test1 = runner.manifest.getRootResource().findResource('test1')
    assert test1

    self.assertEqual(test1.attributes["meetsTheRequirement"], "copy")
    notYetProvided = runner.manifest.specs[0].findMissingProvided(test1)
    self.assertEqual(str(notYetProvided),
"""[<ValidationError: "'copyOfMeetsTheRequirement' is a required property">]""")

    run1 = runner.run(JobOptions(resource='test1'))
    assert not run1.unexpectedAbort, run1.unexpectedAbort.getStackTrace()
    self.assertEqual(test1.attributes['copyOfMeetsTheRequirement'], "copy")

    provided = runner.manifest.specs[0].findMissingProvided(test1)
    assert not provided, provided

    test2 = runner.manifest.getRootResource().findResource('test2')
    assert test2
    requiredAttribute = test2.attributes['meetsTheRequirement']
    assert requiredAttribute is False, requiredAttribute
    missing = runner.manifest.specs[0].findMissingRequirements(test2)
    self.assertEqual(str(missing), '''[<ValidationError: "False is not of type 'string'">]''')

    run2 = runner.run(JobOptions(resource='test2'))
    assert not run2.unexpectedAbort, run2.unexpectedAbort.getStackTrace()
    assert run2.status == Status.degraded, run2.status
    # XXX better error reporting
    # self.assertEqual(str(run2.problems), "can't run required configuration: resource test2 doesn't meet requirement")

  def test_changes(self):
    return
    """
    Test that resource status is updated after the configuration is run and that it doesn't run again
    """
    runner = Runner(manifest)
    run1 = runner.run(resource='test1', startTime = datetime.datetime.fromordinal(1))
    if run1.aborted:
      traceback.print_exception(*run1.aborted) #XXX
    assert not run1.aborted
    assert len(run1.changes) == 1 #XXX1 need to report if configuration changed
    # XXX changeId is 3 because we save after every task?
    self.assertEqual(run1.changes[0].toSource(),
      {'status': 'success', 'changeId': 3, 'commitId': '',
        'startTime': '0001-01-01T00:00:00',
        'action': 'discover', 'metadata': {
        'deleted': [],
        'added': ['copyOfMeetsTheRequirement'],
        'replaced': {}
      }, 'configuration':
        {'name': 'test',
        'digest': 'b43b71275d4c63c259900d4c7083fd8466a0b0bfae102d1f8af9996c0f1979a2'
      }})

    output = six.StringIO()
    runner.manifest.dump(output)
    updatedManifest = output.getvalue()

    runner2 = Runner(updatedManifest)
    #there shouldn't be any tasks to run this time
    run2 = runner2.run(resource='test1', startTime = datetime.datetime.fromordinal(2))
    if run2.aborted:
       traceback.print_exception(*run2.aborted) #XXX
    assert not run2.aborted
    self.assertEqual(len(run2.changes), 0)

    # manifest shouldn't have changed
    output2 = six.StringIO()
    runner2.manifest.dump(output2)
    #round trip testing
    updatedManifest2 = output.getvalue()
    self.assertEqual(updatedManifest, updatedManifest2)

  def test_shouldRun(self):
    pass
    #assert should_run
    # run()
    #assert not should_run

  def test_provides(self):
    #test that it provides as expected
    #test that there's an error if provides fails
    pass

  def test_update(self):
    #test version changed
    pass

  def test_configChanged(self):
    #test version changed
    pass

  def test_revert(self):
    # assert provides is removed
    pass
