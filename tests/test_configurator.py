import unittest
from giterop.manifest import YamlManifest
from giterop.runtime import Runner, Configurator, JobOptions, Status
from giterop.util import lookupPath
import datetime
import logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger('gitup')
logger.setLevel(logging.DEBUG)

class TestConfigurator(Configurator):
  def run(self, task):
    assert self.canRun(task)
    attributes = task.currentConfig.resource.attributes
    attributes['copyOfMeetsTheRequirement'] = attributes["meetsTheRequirement"]
    params = task.currentConfig.parameters
    if params.get('addresources'):
      shouldYield = params.get('yieldresources')
      resourceSpec = {
        'name': params['resourceName'],
        'template': 'resourceTemplate1'
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
      .self: #.self describes the attributes that will apply to the target resource
        attributesSchema:
          properties:
            copyOfMeetsTheRequirement:
              enum: ["copy"]
          required: ['copyOfMeetsTheRequirement']
      .configurations:
        configurationTemplate1:
          +configurations/test:
            raw
          parameters:
            addresources: false
      resourceTemplate1:
        attributes:
          meetsTheRequirement: "yes"
        configurations:
          config1:
            template: configurationTemplate1
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
    test3:
      spec:
        +root/resources/test1/spec:
        configurations:
          test:
            parameters:
              resourceName: added1
              addresources: true
    test4:
      spec:
        +root/resources/test3/spec:
        configurations:
          test:
            parameters:
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

    jobOptions1 = JobOptions(resource='test1', startTime=datetime.datetime.fromordinal(1))
    run1 = runner.run(jobOptions1)
    # print(run1.out.getvalue())
    assert not run1.unexpectedAbort, run1.unexpectedAbort.getStackTrace()
    self.assertEqual(test1.attributes['copyOfMeetsTheRequirement'], "copy")

    provided = runner.manifest.specs[0].findMissingProvided(test1)
    assert not provided, provided

    # check that the modifications were recorded
    self.assertEqual(runner.manifest.manifest['root']['resources']['test1']
          ['status']['configurations']['test']['modifications'],
          {'test1':{'copyOfMeetsTheRequirement': 'copy'}})

    test2 = runner.manifest.getRootResource().findResource('test2')
    assert test2
    requiredAttribute = test2.attributes['meetsTheRequirement']
    assert requiredAttribute is False, requiredAttribute
    missing = runner.manifest.specs[0].findMissingRequirements(test2)
    self.assertEqual(str(missing), '''[<ValidationError: "False is not of type 'string'">]''')

    self.verifyRoundtrip(run1.out.getvalue(), jobOptions1)

    jobOptions2 = JobOptions(resource='test2', startTime=datetime.datetime.fromordinal(1))
    run2 = runner.run(jobOptions2)
    assert not run2.unexpectedAbort, run2.unexpectedAbort.getStackTrace()
    assert run2.status == Status.degraded, run2.status
    # XXX better error reporting
    # self.assertEqual(str(run2.problems), "can't run required configuration: resource test2 doesn't meet requirement")

    # don't re-run the failed configurations so nothing will have changed
    jobOptions2.repair="none"
    self.verifyRoundtrip(run2.out.getvalue(), jobOptions2)

  def test_addingResources(self):
    runner = Runner(YamlManifest(manifest))
    jobOptions = JobOptions(resource='test3', startTime=datetime.datetime.fromordinal(1))
    run = runner.run(jobOptions)
    assert not run.unexpectedAbort, run.unexpectedAbort.getStackTrace()
    self.assertEqual(list(run.workDone.keys()), [('test3', 'test'), ('added1', 'config1')])

    # verify added1
    added = {'.added': {
      'name': 'added1',
      'template': 'resourceTemplate1'
      }
    }
    modifications = lookupPath(runner.manifest.manifest,
    'root.resources.test3.status.configurations.test.modifications'.split('.'))
    self.assertEqual(modifications['added1'], added)
    self.assertEqual(runner.manifest.manifest['changes'][0]['changes']['added1'], added)

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
    dependencies = lookupPath(runner.manifest.manifest,
    'root.resources.test4.status.configurations.test.dependencies'.split('.'))
    self.assertEqual(dependencies, [{"ref": "::added2"}])

    jobOptions.repair="none"
    self.verifyRoundtrip(run.out.getvalue(), jobOptions)

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
