import unittest
import json
import os.path
from unfurl.yamlmanifest import YamlManifest
from unfurl.util import UnfurlError
from unfurl.to_json import to_graphql

def test_jsonexport():
    basepath = os.path.join(os.path.dirname(__file__), "examples/")

    # loads yaml with with a json include
    manifest = YamlManifest(path=basepath + "include-json-ensemble.yaml", skip_validation=True)
    jsonSummary = to_graphql(manifest)
    with open(basepath + "include-json.json") as f:
        assert jsonSummary["ResourceTemplate"] == json.load(f)["ResourceTemplate"]

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
        self.assertIn("missing includes: ['+/templates/production:']", str(err.exception))

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
