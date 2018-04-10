import unittest
from giterop.manifest import *

class ManifestSyntaxTest(unittest.TestCase):
  def test_hasversion(self):
    hasVersion = """
    version: '0.1'
    clusterSpecs:
    clusters:
    """
    assert Manifest(hasVersion)
    badVersion = """
    version: 2
    clusterSpecs:
    clusters:
    """
    with self.assertRaises(GitErOpError):
      Manifest(badVersion)

    missingVersion = """
    clusterSpecs:
    clusters:
    """
    with self.assertRaises(GitErOpError):
      Manifest(missingVersion)

  def test_clusterids(self):
    #clusterids can only contain [a-z0-9_]+
    pass

  def test_cluster_inheritance(self):
    manifest = '''
version: '0.1'
clusterTemplates:
  base:
    components:
      - step1
clusters:
  cloud3: #key is cluster-id
      clusterTemplates:
        - base
        - production
'''
    # error: production clusterTemplate isn't defined
    with self.assertRaises(GitErOpError):
      Manifest(manifest)

    manifest = '''
version: '0.1'
clusterTemplates:
  base:
    components:
      - step1
      - step2
clusters:
  cloud3: #key is cluster-id
      clusterTemplates:
        - base
      # overrides and additions from clusterSpec
      components:
        - name: base.step1
'''
    #overrides base.step1 defination, doesn't add a component
    assert len(Manifest(manifest, validate=False).clusters[0].components) == 2

  def test_override(self):
    #component names have to be qualified to override
    #duplicate names both run with distinct values
    manifest = '''
version: '0.1'
componentSpecs:
  step2:
    actions:
      install: foo
clusterTemplates:
  base:
    components:
      - name: step1
        spec:
          parameters:
            - name: test
              default: default
        parameters:
          test: base
clusters:
  cloud3: #key is cluster-id
      clusterTemplates:
        - base
      # overrides and additions from clusterSpec
      components:
        - name: base.step1
          parameters:
            test: derived
'''
    assert Manifest(manifest).clusters[0].components[0].getParams() == {'test': 'derived'}

  def test_uninstall_override(self):
    #override with action uninstall will just remove base component being applied
    pass

  def test_abbreviations(self):
    # components:
    #   - etcd #equivalent to name: etcd
    #   - name: default-registry #if spec is omitted find componentSpec that matches the name
    pass

  def test_missingSpec(self):
    manifest = '''
version: '0.1'
clusterTemplates:
  base:
    components:
      - step1
      - step2
clusters:
  cloud3: #key is cluster-id
      clusterTemplates:
        - base
      # overrides and additions from clusterSpec
      components:
        - name: base.step1
'''
    with self.assertRaises(GitErOpError) as err:
      Manifest(manifest)
    self.assertEquals(str(err.exception), "component step2 must reference or define a spec")

  def test_badparams(self):
    # don't match spec definition
    manifest = '''
version: '0.1'
componentSpecs:
  step2:
    actions:
      install: foo
clusterTemplates:
  base:
    components:
      - name: step1
        spec:
          parameters:
            - name: test
              default: default
        parameters:
          test: base
clusters:
  cloud3: #key is cluster-id
      clusterTemplates:
        - base
      # overrides and additions from clusterSpec
      components:
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
componentSpecs:
  step2:
    actions:
      install: foo
clusterTemplates:
  base:
    components:
      - name: step1
        spec:
          parameters:
            - name: test
              default: default
        parameters:
          test: base
clusters:
  cloud3: #key is cluster-id
      clusterTemplates:
        - base
      # overrides and additions from clusterSpec
      components:
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
componentSpecs:
  step2:
    actions:
      install: foo
clusterTemplates:
  base:
    components:
      - name: step1
        spec:
          parameters:
            - name: test
              required: True
clusters:
  cloud3: #key is cluster-id
      clusterTemplates:
        - base
      # overrides and additions from clusterSpec
      components:
        - name: base.step1
'''
    with self.assertRaises(GitErOpError) as err:
      Manifest(manifest)
    self.assertEquals(str(err.exception), "missing required parameter: test")
