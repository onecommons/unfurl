import unittest
from giterop import *
from giterop.configurator import *
import traceback
import six
import datetime

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

class RunTest(unittest.TestCase):
  """
  test status: report last state, report config changes, report plan
  test read-ony/discover
  test failing configurations with abort, continue, revert (both capable of reverting and not)
  test incomplete runs
  test unexpected errors, terminations
  test locking
  test required metadata on resources
  """
  
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
