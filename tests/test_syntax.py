import unittest
from unfurl.yamlmanifest import YamlManifest
from unfurl.util import UnfurlError, UnfurlValidationError
from unfurl.localenv import LocalConfig


class ManifestSyntaxTest(unittest.TestCase):
    def test_hasVersion(self):
        hasVersion = """
    apiVersion: unfurl/v1alpha1
    kind: Manifest
    spec: {}
    """
        assert YamlManifest(hasVersion)

    def test_validateVersion(self):
        badVersion = """
    apiVersion: 2
    kind: Manifest
    spec: {}
    """
        with self.assertRaises(UnfurlError) as err:
            YamlManifest(badVersion)
        self.assertIn("apiVersion", str(err.exception))

        missingVersion = """
    spec: {}
    """
        with self.assertRaises(UnfurlError) as err:
            YamlManifest(missingVersion)
        self.assertIn(
            "'apiVersion' is a required property", str(err.exception)
        )  # , <ValidationError: "'kind' is a required property">]''')

    def test_projectconfig(self):
        assert LocalConfig()

    @unittest.skip("TODO")
    def test_validResourceNames(self):
        # should only contain [a-z0-9]([a-z0-9\-]*[a-z0-9])?
        pass

    def test_template_inheritance(self):
        manifest = """
apiVersion: unfurl/v1alpha1
kind: Manifest
templates:
  base:
    configurations:
      step1: {}
spec:
    +/templates/production:
"""
        with self.assertRaises(UnfurlError) as err:
            YamlManifest(manifest)
        self.assertIn("missing includes: [+/templates/production]", str(err.exception))

        manifest = """
apiVersion: unfurl/v1alpha1
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
      +/templates/base:
      # overrides and additions from templates
      configurations:
        step1: {}
"""
        # XXX update yaml
        # overrides base.step1 defination, doesn't add a component
        # manifestObj = YamlManifest(manifest)
        # assert len(manifestObj.rootResource.all['cloud3'].spec['configurations']) == 1, manifestObj.rootResource.all['cloud3'].spec['configurations']

    @unittest.skip("update syntax")
    def test_override(self):
        # component names have to be qualified to override
        # duplicate names both run with distinct values
        manifest = """
apiVersion: unfurl/v1alpha1
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
"""
        configurations = (
            YamlManifest(manifest).rootResource.all["cloud3"].spec["configurations"]
        )
        self.assertEqual(
            list(configurations.values())[0].parameters,
            {
                "test": "derived",
                # '+%': 'replaceProps'
            },
        )

    @unittest.skip("update syntax")
    def test_missingConfigurator(self):
        manifest = """
apiVersion: unfurl/v1alpha1
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

"""
        with self.assertRaises(UnfurlValidationError) as err:
            YamlManifest(manifest)
        self.assertIn(
            str(err.exception.errors),
            """[<ValidationError: "['step1', 'step2'] is not of type 'object'">]""",
        )


#   def test_badparams(self):
#     # don't match spec definition
#     manifest = '''
# apiVersion: unfurl/v1alpha1
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
#     with self.assertRaises(UnfurlValidationError) as err:
#       m = YamlManifest(manifest)
#     self.assertEqual(err and str(err.exception.errors[0][0]), "invalid value")
#
#   def test_unexpectedParam(self):
#     #parameter missing from spec
#     manifest = '''
# apiVersion: unfurl/v1alpha1
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
#     with self.assertRaises(UnfurlValidationError) as err:
#       YamlManifest(manifest)
#     self.assertEqual(str(err.exception.errors[0][0]), "unexpected parameters")
#
#   def test_missingParam(self):
#     #missing required parameter
#     manifest = '''
# apiVersion: unfurl/v1alpha1
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
#     with self.assertRaises(UnfurlValidationError) as err:
#       YamlManifest(manifest)
#     self.assertEqual(str(err.exception.errors[0][0]), "missing required parameter")
