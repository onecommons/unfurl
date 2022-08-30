import unittest
import json
import os.path
from unfurl.yamlmanifest import YamlManifest
from unfurl.util import UnfurlError
from unfurl.to_json import to_deployment
from unfurl.localenv import LocalEnv
from unfurl.planrequests import _find_implementation


def test_jsonexport():
    # XXX cli(unfurl export)
    basepath = os.path.join(os.path.dirname(__file__), "examples/")

    # loads yaml with with a json include
    local = LocalEnv(basepath + "include-json-ensemble.yaml")
    # verify to_graphql is working as expected
    jsonExport = to_deployment(local)
    with open(basepath + "include-json.json") as f:
        expected = json.load(f)
        # print(json.dumps(jsonExport, indent=2))
        assert jsonExport["ResourceTemplate"] == expected["ResourceTemplate"]
        # test environmentVariableNames export
        assert jsonExport["DeploymentTemplate"]["unnamed"]["environmentVariableNames"] == [
                                                  "APP_IMAGE",
                                                  "APP_DOMAIN",
                                                  "HOST_CPUS",
                                                  "HOST_MEMORY",
                                                  "HOST_STORAGE",
                                                  "RESOLVER_CONSOLE_URL"
                                                ]
        resource = jsonExport["Resource"]["foo.com"]
        assert resource
        # test explicit "export" metadata
        assert resource["attributes"][0]["name"] == "console_url"
        # make sure console_url doesn't also appear here:
        assert not resource["computedProperties"]

    # XXX verify that saving the manifest preserves the json include


class ManifestSyntaxTest(unittest.TestCase):
    def test_hasVersion(self):
        hasVersion = """
    apiVersion: unfurl/v1alpha1
    kind: Ensemble
    spec: {}
    """
        assert YamlManifest(hasVersion)

    def test_validateVersion(self):
        badVersion = """
    apiVersion: 2
    kind: Ensemble
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

    def test_missing_includes(self):
        manifest = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
templates:
  base:
    configurations:
      step1: {}
spec:
    +/templates/production:
"""
        with self.assertRaises(UnfurlError) as err:
            YamlManifest(manifest)
        self.assertIn(
            "missing includes: ['+/templates/production:']", str(err.exception)
        )

    def test_template_inheritance(self):
        manifest = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
spec:
  service_template:
    node_types:
      Base:
        derived_from: tosca:Root
        interfaces:
          Standard:
            operations:
              create:
              configure:
        requirements:
        - host:
            metadata:
              base: meta
              title: base host
            relationship: tosca.relationships.DependsOn
            description: A base compute instance
            node: Base

      Derived:
        derived_from: Base
        interfaces:
          Standard:
            operations:
              create:
        requirements:
        - host:
            metadata:
              title: derived host
            description: A derived compute instance
            node: Derived

    topology_template:
      node_templates:
        the_app:
          type: Derived
          requirements:
          - host:
              node: the_app
"""
        manifest = YamlManifest(manifest)
        app_template = manifest.tosca.nodeTemplates['the_app']
        req_def = app_template.toscaEntityTemplate.type_definition.requirement_definitions['host']
        assert _find_implementation("Standard", "configure", app_template)
        
        assert req_def == {
          'metadata': {'base': 'meta', 'title': 'derived host'},
          'relationship': {'type': 'tosca.relationships.DependsOn'},
          'description': 'A derived compute instance',
          'node': 'Derived'
        }

class ToscaSyntaxTest(unittest.TestCase):

    def test_bad_interface_on_type(self):
        manifest = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
spec:
  service_template:
    node_types:
      Base:
        derived_from: tosca:Root
        interfaces:
          defaults:
            resultTemplate:
              wrongplace

      Derived:
        derived_from: Base
        interfaces:
          Standard:
            create:

    topology_template:
      node_templates:
        the_app: # type needs to be instantiated to trigger validation
          type: Derived
"""
        with self.assertRaises(UnfurlError) as err:
            YamlManifest(manifest)
        self.assertIn(
            'type "Base" contains unknown field "resultTemplate"', str(err.exception)
        )

    def test_property_default_null(self):
        manifest = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
spec:
  instances:
    the_app:
      template: the_app
  service_template:
    node_types:
      Base:
        derived_from: tosca:Root
        properties:
          null_default:
            type: string
            default: null
            required: false

    topology_template:
      node_templates:
        the_app:
          type: Base
"""
        ensemble = YamlManifest(manifest)
        root = ensemble.get_root_resource()
        the_app = root.find_instance("the_app")
        assert the_app
        assert the_app.attributes["null_default"] is None
