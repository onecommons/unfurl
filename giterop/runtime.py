import six

class JobOptions(object):
  """
  Options available to select which tasks are run, e.g. read-only

  does the config apply to the action?
  is it out of date?
  is it in a ok state?
  """
  defaults = dict(
    resource=None, # resource filter
    configuration=None, # configuration filter

    add=True, # run newly added configurations
    upgrade=False, # run configurations that versions changed and whose spec has changed
    update=True, # run configurations that whose spec has changed but don't a major version chang
    repair='error' # or 'degraded', run configurations that are not operational and/or degraded
    all=False, # run all
    verify=False, # discover first and set status if it differs from expected state
    readOnly=False, # only run configurations that won't alter the system

    dryRun=False,
    requiredOnly=False,
    purgeObsolete=False,
    purgeOrphans=False)

  def __init__(self, **kw):
    self.__dict__.update(kw.update(self.defaults))

class Runner(object):
  def createJob(self, joboptions):
    """
    Selects task to run based on job options and starting state of manifest
    """
    return job

  def shouldRunTask(self, task):
    """
    Checked at runtime right before each task is run

    * check "when" conditions to see if it should be run
    * check task if it should be run
    """
    return task

  def canRunTask(self, task):
    """
    Checked at runtime right before each task is run

    * check "required"/pre-conditions to see if it can be run
    * check task if it can be run
    """
    return task

  def checkStatusAfterRun(self, task, change):
    """
    After each task has run:
    * Check that it provided the metadata it declared it would provide
    * Check dependencies:
    ** check runtime-(post-)conditions of still hold for configurations that might have been affected by changes
    ** check for configurations whose inputs might have been affected by changes, mark them as "configuration changed"
    (simple implementation for both: check all configurations (requires saving previous inputs))
    ** check for orphaned resources and mark them as orphaned
      (a resource is orphaned if it was added as a dependency and no longer has dependencies)
      (orphaned resources can be deleted by the configuration/configurator that created them or manages that type)
    """
    return change

  def run(self, joboptions):
    job = self.createJob(joboptions)
    for task in job.getCandidateTasks():
      if not self.shouldRunTask(task):
        self.markSkipped(task)
        continue
      if not self.canRunTask(task):
        # XXX:
        self.markFailed(task)
        continue

      change = job.runTask(task)
      # if XXX error
      # on failure just abort run or skip
      self.checkStatusAfterRun(task, change)
      self.save(task, change)

      if self.shouldAbort(task, change):
        break

    if job.modifiedState:
      self.saveJob(job)

class Change(object):
  def __init__(self, name):
    self.name = name

  def __str__(self):
    return "A Change: " + self.name

class Configuration(object):
  def __init__(self, configuratorGenerator, name = ''):
    self.generator = configuratorGenerator
    self.name = name

  def run(self, task):
    return self.generator(task)

  def __str__(self):
    return "Configuration: " + self.name

class Task(object):
  def __init__(self, configuration):
    self.configuration = configuration

  def run(self):
    return self.configuration.run(self)

  def createResource(self, resource):
    return Task(self, resource)

  def createSubTask(self, configuration):
    return Task(configuration)

  def __str__(self):
    return "Task: " + str(self.configuration)

class Job(object):
  def __init__(self, jobOptions):
    self.__dict__.update(jobOptions)

  def runTask(self, task):
    """
    During each task run:
    * Notification of metadata changes that reflect changes made to resources
    * Notification of add or removing dependency on a resource or properties of a resource
    * Notification of creation or deletion of a resource
    * Requests a resource with requested metadata, if it doesn't exist, a task is run to make it so
    (e.g. add a dns entry, install a package).
    XXX need a way for configurator to declare that is the manager of a particular resource or type of resource or metadata so we know to handle that request
    """
  # XXX recursion or loop detection
  generator = task.run()
  change = None
  while True:
    result = generator.send(change)
    if isinstance(result, Change):
      generator.close()
      return result
    elif isinstance(result, Task):
      change = self.runTask(result)
    else:
      raise 'unexpected result'

  def includeTask(self, config, lastChange):
    """
spec (config):
  intent: discover instantiate revert
  config
  version

status (lastChange):
  state: ok degraded error notpresent
  compared to spec: same different missing orphan
  action: discover instantiate revert

status compared to spec is different: compare difference for each:
  config
  intent vs. action: (d, i) (i, d) (i, r) (r, i) (d, r) (r, d)
  version: newer older

Note: dependency changes (where input values changed even if config spec hasn't) are purely runtime they need to discovered
(declared dependency changes are essentially config changes)
    """
    assert config or lastChange
    if self.all and config:
      return config
    if config and not lastChange:
      if self.add:
        return config
      else:
        return None
    elif lastChange and not config:
      if self.purgeObsolete:
        return lastChange.config, 'revert'
      if self.all:
        return lastChange
    elif self.isDifferent(lastChange, config):
      if config.intent == 'revert' and lastChange.status == 'notpresent':
        return None
      if self.upgrade:
        return config
      #this case is essentially a re-added config:
      if lastChange.status == 'notpresent' and lastChange.config.intent != config.intent and self.add:
        return config
      if self.update:
        if config.intent != 'revert' and not self.majorVersionDifference(lastChange.config, config):
          return config
    return self.checkForRepair(lastChange)

  def checkForRepair(self, lastChange):
    if lastChange.state == 'ok' or not self.repair:
        return None
    if self.repair == 'degraded':
      return lastChange
    elif lastChange.state == 'degraded':
      assert self.repair == 'error'
      return None
    else:
      assert self.repair == 'error'
      return lastChange

  def getTasks(self):
    """
    Find candidate tasks

    Given declared spec, current status, and job options, generate selector

    does the config apply to the action?
    is it out of date?
    is it in a ok state?
    has its configuration changed?
    has its dependencies changed?
    are the resources it modifies in need of repair?
    manual override (include / skip)


    # intent: discover instantiate revert
    # version
    # configuration
    """
    changes = self.getChanges()
    for config in configs:
      lastChange = self.popLastChange(changes, config)
      config = self.includeTask(config, lastChange)
      if not self.filterConfig(config):
        yield config
    for change in changes:
      # orphaned changes
      config = self.includeTask(None, change)
      if not self.filterConfig(config):
        yield config

  def filterConfig(self, config)
      if self.readOnly and config.intent != 'discover':
        return None
      if self.requiredOnly and not config.required:
        return None
      return config

def testGenerator(task):
  """
  Run is a generator, yields the result of running the task or a subtask
  """
  status = yield task.createSubTask(TestConfig("subtask"))
  print('got status ' + str(status))
  #if (status.failed)
  #yield 'huh'
  yield Change('testGenerator')

class TestConfig(Configuration):
  def generator(self, task):
    yield Change(self.name)

  def __init__(self, name):
    super(TestConfig, self,).__init__(self.generator, name)

def test():
  return Job().runTask(Task(Configuration(testGenerator, 'start')))

print('runtask ' + str(test()))
