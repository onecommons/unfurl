import giterop.util
import unittest
import os
import os.path
import warnings
from giterop import *
from giterop.ansibleconfigurator import *

class AnsibleTest(unittest.TestCase):
  def setUp(self):
    # need to call this again on python 2.7:
    giterop.util.initializeAnsible()
    try:
      # Ansible generates tons of ResourceWarnings
      warnings.simplefilter("ignore", ResourceWarning)
    except:
      # python 2.x doesn't have ResourceWarning
      pass
    self.results = {}

  def runPlaybook(self, args=None):
    return runPlaybooks([os.path.join(os.path.dirname(__file__),'testplaybook.yaml')], 'localhost,', {
    'ansible_connection': 'local',
    'extra': 1
    }, args)

  def test_runplaybook(self):
    results = self.runPlaybook()
    self.results['runplaybook'] = results
    self.assertEqual('test',
      results.variableManager._nonpersistent_fact_cache['localhost'].get('one_fact'))

    hostfacts = results.variableManager._nonpersistent_fact_cache['localhost'];
    self.assertEqual(hostfacts['one_fact'], 'test')
    self.assertEqual(hostfacts['echoresults']['stdout'], "hello")

  def test_verbosity(self):
    results = self.runPlaybook()
    # task test-verbosity was skipped
    assert not results.resultsByStatus.ok.get('test-verbosity')
    assert results.resultsByStatus.skipped.get('test-verbosity')

    results = self.runPlaybook(['-vv'])
    # task test-verbosity was ok this time
    assert results.resultsByStatus.ok.get('test-verbosity')
    assert not results.resultsByStatus.skipped.get('test-verbosity')
