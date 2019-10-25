import unittest
from unfurl.yamlmanifest import YamlManifest
from unfurl.job import Runner, JobOptions, Status
from unfurl.configurator import Configurator
from unfurl.util import lookupPath
import datetime

class TestConfigurator(Configurator):
  def run(self, task):
    assert not self.cantRun(task)
    attributes = task.target.attributes
    attributes['copyOfMeetsTheRequirement'] = attributes["meetsTheRequirement"]
    params = task.inputs
    if params.get('addresources'):
      shouldYield = params.get('yieldresources')
      resourceSpec = {
        'name': params['resourceName'],
        'template': 'test1'
      }
      if shouldYield:
        resourceSpec['dependent'] = True

      updateSpec = dict(name='.self', status=dict(attributes={'newAttribute': 1}))
      jobrequest = task.addResources([resourceSpec, updateSpec])
      if shouldYield:
        job = yield jobrequest
        assert job, jobrequest

    yield task.createResult(True, True, Status.ok)

manifest = '''
apiVersion: unfurl/v1alpha1
kind: Manifest
spec:
 implementations:
  test:
    apiVersion: unfurl/v1alpha1
    className: TestConfigurator
    majorVersion: 0
    preConditions:
      properties:
        meetsTheRequirement:
          type: string
      required: ['meetsTheRequirement']
 instances:
    test1:
        attributes:
          meetsTheRequirement: "copy"
        implementations:
          create: test
    test2:
        attributes:
          meetsTheRequirement: false
        implementations:
          create: test
    test3:
        +spec/instances/test1:
        implementations:
          create:
            implementation:
              primary: test
            inputs:
              resourceName: added1
              addresources: true
    test4:
        +spec/instances/test3:
        implementations:
          create:
            implementation:
              primary: test
            inputs:
              resourceName: added2
              yieldresources: true
'''

class ConfiguratorTest(unittest.TestCase):
  def verifyRoundtrip(self, original, jobOptions):
    job = Runner(YamlManifest(original)).run(jobOptions)
    # should not need to run any tasks
    assert len(job.workDone) == 0, job.workDone

    self.maxDiff = None
    self.assertEqual(original, job.out.getvalue())

  def test_neededTasks(self):
    """
    test that runner figures out the proper tasks to run
    """
    runner = Runner(YamlManifest(manifest))
    test1 = runner.manifest.getRootResource().findResource('test1')
    assert test1
    # missing = runner.manifest.spec[0].findInvalidPreconditions(test1)
    # assert not missing, missing

    run1 = runner.run(JobOptions(resource='test1'))
    assert len(run1.workDone) == 1, run1.workDone
    assert not run1.unexpectedAbort, run1.unexpectedAbort.getStackTrace()

  def test_preConditions(self):
    """test that the configuration only runs if the resource meets the requirements"""
    runner = Runner(YamlManifest(manifest))
    test1 = runner.manifest.getRootResource().findResource('test1')
    assert test1

    self.assertEqual(test1.attributes["meetsTheRequirement"], "copy")
#     notYetProvided = runner.manifest.spec[0].findMissingProvided(test1)
#     self.assertEqual(str(notYetProvided[1]),
# """[<ValidationError: "'copyOfMeetsTheRequirement' is a required property">]""")

    jobOptions1 = JobOptions(resource='test1', startTime=datetime.datetime.fromordinal(1))
    run1 = runner.run(jobOptions1)
    # print(run1.out.getvalue())
    assert not run1.unexpectedAbort, run1.unexpectedAbort.getStackTrace()
    self.assertEqual(test1.attributes['copyOfMeetsTheRequirement'], "copy")

    # provided = runner.manifest.spec[0].findMissingProvided(test1)
    # assert not provided, provided

    # check that the modifications were recorded
    self.assertEqual(runner.manifest.manifest.config['changes'][0]['changes'],
          {'::test1':{'copyOfMeetsTheRequirement': 'copy', '.status': 'ok'}})

    test2 = runner.manifest.getRootResource().findResource('test2')
    assert test2
    requiredAttribute = test2.attributes['meetsTheRequirement']
    assert requiredAttribute is False, requiredAttribute
    # missing = runner.manifest.specs[0].findMissingRequirements(test2)
    # self.assertEqual(str(missing[1]), '''[<ValidationError: "False is not of type 'string'">]''')

    self.verifyRoundtrip(run1.out.getvalue(), jobOptions1)

    jobOptions2 = JobOptions(resource='test2', startTime=datetime.datetime.fromordinal(1))
    run2 = runner.run(jobOptions2)
    assert not run2.unexpectedAbort, run2.unexpectedAbort.getStackTrace()
    assert run2.status == Status.error, run2.status
    # XXX better error reporting
    # self.assertEqual(str(run2.problems), "can't run required configuration: resource test2 doesn't meet requirement")

    # don't re-run the failed configurations so nothing will have changed
    jobOptions2.repair="none"
    self.verifyRoundtrip(run2.out.getvalue(), jobOptions2)

  @unittest.skip("fix adding resources api")
  def test_addingResources(self):
    runner = Runner(YamlManifest(manifest))
    jobOptions = JobOptions(resource='test3', startTime=datetime.datetime.fromordinal(1))
    run = runner.run(jobOptions)
    assert not run.unexpectedAbort, run.unexpectedAbort.getStackTrace()
    # self.assertEqual(list(run.workDone.keys()), [('test3', 'test'), ('added1', 'config1')])

    # verify added1
    added = {'.added': {
      'name': 'added1',
      'template': 'test1'
      }
    }
    modifications = lookupPath(runner.manifest.manifest.config,
    'changes.test.modifications'.split('.'))
    self.assertEqual(modifications['added1'], added)
    self.assertEqual(runner.manifest.manifest.config['changes'][0]['changes']['added1'], added)

    # verify modified
    self.assertEqual(modifications['test3'], {
      'copyOfMeetsTheRequirement': 'copy',
      'newAttribute': 1
    })

    # print('test3', run.out.getvalue())
    jobOptions.repair="none"
    self.verifyRoundtrip(run.out.getvalue(), jobOptions)

    jobOptions = JobOptions(resource='test4', startTime=datetime.datetime.fromordinal(1))
    run = runner.run(jobOptions)
    assert not run.unexpectedAbort, run.unexpectedAbort.getStackTrace()
    self.assertEqual(list(run.workDone.keys()), [('test4', 'test'), ('added2', 'config1')])
    # print('test4', run.out.getvalue())

    # verify dependencies added
    dependencies = lookupPath(runner.manifest.manifest.config,
    'root.resources.test4.status.configurations.test.dependencies'.split('.'))
    self.assertEqual(dependencies, [{"ref": "::added2"}])

    jobOptions.repair="none"
    self.verifyRoundtrip(run.out.getvalue(), jobOptions)

  # def test_shouldRun(self):
  #   pass
  #   #assert should_run
  #   # run()
  #   #assert not should_run
  #
  # def test_provides(self):
  #   #test that it provides as expected
  #   #test that there's an error if provides fails
  #   pass
  #
  # def test_update(self):
  #   #test version changed
  #   pass
  #
  # def test_configChanged(self):
  #   #test version changed
  #   pass
  #
  # def test_revert(self):
  #   # assert provides is removed
  #   pass
