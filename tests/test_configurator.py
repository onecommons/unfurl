import unittest
from giterop import *
from giterop.configurator import *
import traceback
import six
import datetime

class TestConfigurator(Configurator):
  def run(self, task):
    assert self.canRun(task)
    task.resource.metadata['copyOfMeetsTheRequirement'] = task.resource.metadata["meetsTheRequirement"]
    return self.status.success

registerClass("giterops/v1alpha1", "TestConfigurator", TestConfigurator)

manifest = '''
apiVersion: giterops/v1alpha1
kind: Manifest
configurators:
  test:
    apiVersion: giterops/v1alpha1
    kind: TestConfigurator
    requires:
      - name: meetsTheRequirement
        type: string
    provides:
      - name: copyOfMeetsTheRequirement
        always: copy
        required: True
templates:
  test:
      configurations:
        - configurator: test
resources:
  test1:
    metadata:
      meetsTheRequirement: "copy"
    spec:
      templates:
        - test
  test2:
    metadata:
      meetsTheRequirement: false
    spec:
      templates:
        - test
'''

class ConfiguratorTest(unittest.TestCase):
  def test_neededTasks(self):
    runner = Runner(manifest)
    resources = runner.getRootResources('test1')
    assert resources, "couldn't find root resource test1"
    assert len(resources) == 1, resources
    test1 = resources[0]
    missing = test1.definition.spec.configurations[0].configurator.findMissingRequirements(test1)
    assert not missing, missing

    #print resources[0].spec.configurations
    tasks = list(runner.getTasks(resources))
    assert tasks and len(tasks) == 1, tasks

  def test_requires(self):
    #test that it the configurator only runs if the resource meets the requirements
    runner = Runner(manifest)
    test1 = runner.manifest.getRootResource('test1')
    assert test1

    self.assertEqual(test1.metadata["meetsTheRequirement"], "copy")
    configurator = runner.manifest.configurators['test']
    notYetProvided = configurator.findMissingProvided(test1)
    self.assertEqual(notYetProvided, [('missing required parameter', 'copyOfMeetsTheRequirement')])

    assert runner.run(resource='test1'), runner.aborted
    self.assertEqual(test1.metadata['copyOfMeetsTheRequirement'], "copy")

    provided = configurator.findMissingProvided(test1)
    assert not provided, provided

    test2 = runner.manifest.getRootResource('test2')
    assert test2
    requiredAttribute = test2.metadata['meetsTheRequirement']
    assert requiredAttribute is False, requiredAttribute

    missing = test2.spec.configurations[0].configurator.findMissingRequirements(test2)
    assert missing, missing

    # XXX should resport that test configuration failed because test2 didn't meet the requirements
    result = runner.run(resource='test2')
    assert not result, result
    #XXX bad error reporting
    self.assertEqual(str(runner.aborted), "cannot run")
    #self.assertEqual(str(runner.aborted), "can't run required configuration: resource test2 doesn't meet requirement")

  def test_changes(self):
    runner = Runner(manifest)
    runner.run(resource='test1', startTime = datetime.datetime.fromordinal(1))
    if runner.aborted:
      traceback.print_exception(*runner.aborted)
    assert not runner.aborted
    assert len(runner.changes) == 1
    # XXX changeId is 3 because we save after every task?
    self.assertEqual(runner.changes[0].toSource(),
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
    runner2.run(resource='test1', startTime = datetime.datetime.fromordinal(2))
    if runner2.aborted:
       traceback.print_exception(*runner2.aborted)
    assert not runner2.aborted
    self.assertEqual(len(runner2.changes), 0)

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
