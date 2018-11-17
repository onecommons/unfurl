import unittest
from giterop.manifest import YamlManifest
from giterop.util import GitErOpError, GitErOpValidationError

class ManifestSyntaxTest(unittest.TestCase):
  def test_hasversion(self):
    hasVersion = """
    apiVersion: giterops/v1alpha1
    kind: Manifest
    root: {}
    """
    assert YamlManifest(hasVersion)

    badVersion = """
    apiVersion: 2
    kind: Manifest
    root: {}
    """
    with self.assertRaises(GitErOpError) as err:
      YamlManifest(badVersion)
    self.assertEqual(str(err.exception), '[<ValidationError: "2 is not one of [\'giterops/v1alpha1\']">]')

    missingVersion = """
    root: {}
    """
    with self.assertRaises(GitErOpError) as err:
      YamlManifest(missingVersion)
    self.assertEqual(str(err.exception), '''[<ValidationError: "'apiVersion' is a required property">, <ValidationError: "'kind' is a required property">]''')

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
      step1: {}
root:
    +templates/production:
'''
    with self.assertRaises(GitErOpError) as err:
      YamlManifest(manifest)
    self.assertEqual(str(err.exception), 'can not find "templates/production" in document')

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
      step1:
        className: foo
        majorVersion: 0
root:
 resources:
  cloud3:
    spec:
      +templates/base:
      # overrides and additions from templates
      configurations:
        step1: {}
'''
    #overrides base.step1 defination, doesn't add a component
    manifestObj = YamlManifest(manifest)
    assert len(manifestObj.rootResource.all['cloud3'].spec['configurations']) == 1, manifestObj.rootResource.all['cloud3'].spec['configurations']

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
      step1:
        className: foo
        majorVersion: 0
        configurator:
          parameterSchema:
            - name: test
              default: default
        parameters:
          test:
            base
root:
 resources:
  cloud3:
    spec:
      templates:
        - base
      # overrides and additions from template
      configurations:
        +templates/base/configurations:
        step1:
          parameters:
            test: derived
'''
    configurations = YamlManifest(manifest).rootResource.all['cloud3'].spec['configurations']
    self.assertEqual(list(configurations.values())[0].parameters, {
      'test': 'derived',
      # '+%': 'replaceProps'
    })

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
root:
 resources:
  cloud3:
    spec:
      templates:
        - base
      configurations:
        +templates/base/configurations:

'''
    with self.assertRaises(GitErOpError) as err:
      YamlManifest(manifest)
    self.assertEqual(str(err.exception), '''[<ValidationError: "['step1', 'step2'] is not of type 'object'">]''')

#   def test_badparams(self):
#     # don't match spec definition
#     manifest = '''
# apiVersion: giterops/v1alpha1
# kind: Manifest
#
# configurators:
#   step2:
#     actions:
#       install: foo
# templates:
#   base:
#     configurations:
#       - name: step1
#         configurator:
#           parameterSchema:
#             - name: test
#               type: string
#               default: default
#         parameters:
#           test: base
# resources:
#   cloud3:
#     spec:
#       templates:
#         - base
#       # overrides and additions
#       configurations:
#         - name: base.step1
#           parameters:
#             # error: should be a string
#             test: 0
# '''
#     with self.assertRaises(GitErOpValidationError) as err:
#       m = YamlManifest(manifest)
#     self.assertEqual(err and str(err.exception.errors[0][0]), "invalid value")
#
#   def test_unexpectedParam(self):
#     #parameter missing from spec
#     manifest = '''
# apiVersion: giterops/v1alpha1
# kind: Manifest
#
# configurators:
#   step2:
#     actions:
#       install: foo
# templates:
#   base:
#     configurations:
#       - name: step1
#         configurator:
#           parameterSchema:
#             - name: test
#               default: default
#         parameters:
#           test: base
# resources:
#   cloud3:
#     spec:
#       templates:
#         - base
#       # overrides and additions
#       configurations:
#         - name: base.step1
#           parameters:
#             doesntexist: True
# '''
#     with self.assertRaises(GitErOpValidationError) as err:
#       YamlManifest(manifest)
#     self.assertEqual(str(err.exception.errors[0][0]), "unexpected parameters")
#
#   def test_missingParam(self):
#     #missing required parameter
#     manifest = '''
# apiVersion: giterops/v1alpha1
# kind: Manifest
#
# configurators:
#   step2:
#     actions:
#       install: foo
# templates:
#   base:
#     configurations:
#       - name: step1
#         configurator:
#           parameterSchema:
#             - name: test
#               required: True
# resources:
#   cloud3:
#     spec:
#       templates:
#         - base
#       # overrides and additions
#       configurations:
#         - name: base.step1
# '''
#     with self.assertRaises(GitErOpValidationError) as err:
#       YamlManifest(manifest)
#     self.assertEqual(str(err.exception.errors[0][0]), "missing required parameter")
