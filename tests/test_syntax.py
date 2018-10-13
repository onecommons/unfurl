import unittest
from giterop.run import *

class ManifestSyntaxTest(unittest.TestCase):
  def test_hasversion(self):
    hasVersion = """
    apiVersion: giterops/v1alpha1
    kind: Manifest
    resources:
    """
    assert Manifest(hasVersion)

    badVersion = """
    apiVersion: 2
    kind: Manifest
    resources:
    """
    with self.assertRaises(GitErOpError) as err:
      Manifest(badVersion)
    self.assertEqual(str(err.exception), "unknown version: 2")

    missingVersion = """
    resources:
    """
    with self.assertRaises(GitErOpError) as err:
      Manifest(missingVersion)
    self.assertEqual(str(err.exception), "missing version")

  def test_resourcenames(self):
    #clusterids can only contain [a-z0-9]([a-z0-9\-]*[a-z0-9])?
    pass

  def test_template_inheritance(self):
    manifest = '''
apiVersion: giterops/v1alpha1
kind: Manifest
configurators:
  step1:
    actions:
      install: foo
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
    self.assertEqual(str(err.exception), "template reference is not defined: production")

    manifest = '''
apiVersion: giterops/v1alpha1
kind: Manifest

configurators:
  step1:
    actions:
      install: foo
  step2:
    actions:
      install: foo
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
    assert len(manifestObj.resources[0].spec.configurations) == 2, manifestObj.resources[0].configuration.configurations

  def test_override(self):
    #component names have to be qualified to override
    #duplicate names both run with distinct values
    manifest = '''
apiVersion: giterops/v1alpha1
kind: Manifest

configurators:
  step2:
    actions:
      install: foo
templates:
  base:
    templates:
    configurations:
      - name: step1
        configurator:
          parameterSchema:
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
    assert Manifest(manifest).resources[0].spec.configurations[0].getParams() == {'test': 'derived'}

  def test_uninstall_override(self):
    #override with action uninstall will just remove base component being applied
    pass

  def test_abbreviations(self):
    # configurations:
    #   - etcd #equivalent to name: etcd
    #   - name: default-registry #if spec is omitted find componentSpec that matches the name
    pass

  def test_missingConfigurator(self):
    manifest = '''
apiVersion: giterops/v1alpha1
kind: Manifest

configurators:
  step2:
    actions:
      install: foo
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
      m = Manifest(manifest)
    self.assertEqual(str(err.exception), "configurator not found: step1")

  def test_badparams(self):
    # don't match spec definition
    manifest = '''
apiVersion: giterops/v1alpha1
kind: Manifest

configurators:
  step2:
    actions:
      install: foo
templates:
  base:
    configurations:
      - name: step1
        configurator:
          parameterSchema:
            - name: test
              type: string
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
    with self.assertRaises(GitErOpValidationError) as err:
      m = Manifest(manifest)
    self.assertEqual(err and str(err.exception.errors[0][0]), "invalid value")

  def test_unexpectedParam(self):
    #parameter missing from spec
    manifest = '''
apiVersion: giterops/v1alpha1
kind: Manifest

configurators:
  step2:
    actions:
      install: foo
templates:
  base:
    configurations:
      - name: step1
        configurator:
          parameterSchema:
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
    with self.assertRaises(GitErOpValidationError) as err:
      Manifest(manifest)
    self.assertEqual(str(err.exception.errors[0][0]), "unexpected parameters")

  def test_missingParam(self):
    #missing required parameter
    manifest = '''
apiVersion: giterops/v1alpha1
kind: Manifest

configurators:
  step2:
    actions:
      install: foo
templates:
  base:
    configurations:
      - name: step1
        configurator:
          parameterSchema:
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
    with self.assertRaises(GitErOpValidationError) as err:
      Manifest(manifest)
    self.assertEqual(str(err.exception.errors[0][0]), "missing required parameter")
