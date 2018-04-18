import unittest
from giterop.manifest import *

class ManifestSyntaxTest(unittest.TestCase):
  def test_hasversion(self):
    hasVersion = """
    version: '0.1'
    resources:
    """
    assert Manifest(hasVersion)

    badVersion = """
    version: 2
    resources:
    """
    with self.assertRaises(GitErOpError) as err:
      Manifest(badVersion)
    self.assertEquals(str(err.exception), "unknown version: 2")

    missingVersion = """
    resources:
    """
    with self.assertRaises(GitErOpError) as err:
      Manifest(missingVersion)
    self.assertEquals(str(err.exception), "missing version")

  def test_resourcenames(self):
    #clusterids can only contain [a-z0-9]([a-z0-9\-]*[a-z0-9])?
    pass

  def test_template_inheritance(self):
    manifest = '''
version: '0.1'
templates:
  base:
    configurations:
      - step1
resources:
  cloud3: #key is resource name
    spec:
      templates:
        - base
        - production
'''
    with self.assertRaises(GitErOpError) as err:
      Manifest(manifest)
    self.assertEquals(str(err.exception), "template reference is not defined: production")

    manifest = '''
version: '0.1'
templates:
  base:
    configurations:
      - step1
      - step2
resources:
  cloud3:
    spec:
      templates:
        - base
      # overrides and additions from templates
      configurations:
        - name: base.step1
'''
    #overrides base.step1 defination, doesn't add a component
    manifestObj = Manifest(manifest, validate=False)
    assert len(manifestObj.resources[0].configuration.configurations) == 2, manifestObj.resources[0].configuration.configurations

  def test_override(self):
    #component names have to be qualified to override
    #duplicate names both run with distinct values
    manifest = '''
version: '0.1'
configurators:
  step2:
    actions:
      install: foo
templates:
  base:
    templates:
    configurations:
      - name: step1
        spec:
          parameters:
            - name: test
              default: default
        parameters:
          test: base
resources:
  cloud3:
    spec:
      templates:
        - base
      # overrides and additions from template
      configurations:
        - name: base.step1
          parameters:
            test: derived
'''
    assert Manifest(manifest).resources[0].configuration.configurations[0].getParams() == {'test': 'derived'}

  def test_uninstall_override(self):
    #override with action uninstall will just remove base component being applied
    pass

  def test_abbreviations(self):
    # configurations:
    #   - etcd #equivalent to name: etcd
    #   - name: default-registry #if spec is omitted find componentSpec that matches the name
    pass

  def test_missingSpec(self):
    manifest = '''
version: '0.1'
templates:
  base:
    configurations:
      - step1
      - step2
resources:
  cloud3:
    spec:
      templates:
        - base
      configurations:
        - name: base.step1
'''
    with self.assertRaises(GitErOpError) as err:
      Manifest(manifest)
    self.assertEquals(str(err.exception), "configuration step1 must reference or define a spec")

  def test_badparams(self):
    # don't match spec definition
    manifest = '''
version: '0.1'
configurators:
  step2:
    actions:
      install: foo
templates:
  base:
    configurations:
      - name: step1
        spec:
          parameters:
            - name: test
              default: default
        parameters:
          test: base
resources:
  cloud3:
    spec:
      templates:
        - base
      # overrides and additions
      configurations:
        - name: base.step1
          parameters:
            # error: should be a string
            test: 0
'''
    with self.assertRaises(GitErOpError) as err:
      Manifest(manifest)
    self.assertEquals(str(err.exception), "invalid value: test")

    #parameter missing from spec
    manifest = '''
version: '0.1'
configurators:
  step2:
    actions:
      install: foo
templates:
  base:
    configurations:
      - name: step1
        spec:
          parameters:
            - name: test
              default: default
        parameters:
          test: base
resources:
  cloud3:
    spec:
      templates:
        - base
      # overrides and additions
      configurations:
        - name: base.step1
          parameters:
            doesntexist: True
'''
    with self.assertRaises(GitErOpError) as err:
      Manifest(manifest)
    self.assertEquals(str(err.exception), "unexpected parameter(s): ['doesntexist']")

    #missing required parameter
    manifest = '''
version: '0.1'
configurators:
  step2:
    actions:
      install: foo
templates:
  base:
    configurations:
      - name: step1
        spec:
          parameters:
            - name: test
              required: True
resources:
  cloud3:
    spec:
      templates:
        - base
      # overrides and additions
      configurations:
        - name: base.step1
'''
    with self.assertRaises(GitErOpError) as err:
      Manifest(manifest)
    self.assertEquals(str(err.exception), "missing required parameter: test")
