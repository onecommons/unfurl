import pprint
import unittest
import json
import os.path

import pytest
from unfurl.yamlmanifest import YamlManifest
from unfurl.util import UnfurlError
from unfurl.to_json import to_blueprint, to_deployment
from unfurl.localenv import LocalEnv
from unfurl.planrequests import _find_implementation


def test_jsonexport():
    # XXX cli(unfurl export)
    basepath = os.path.join(os.path.dirname(__file__), "examples/")

    # loads yaml with with a json include
    local = LocalEnv(basepath + "include-json-ensemble.yaml")
    # verify to_graphql is working as expected
    jsonExport = to_deployment(local)

    # check that subtypes can remove inherited operations if marked not_implemented
    assert jsonExport["ResourceType"]["Atlas"]["implementations"] == ["configure"]
    # check the types aren't exported if not referenced by a template
    assert "SelfHostedMongoDb" not in jsonExport["ResourceType"]
    # check that subtypes inherit interface requirements
    assert jsonExport["ResourceType"]["Atlas"]["implementation_requirements"] == [
        "unfurl.relationships.ConnectsTo.AWSAccount"
    ]

    with open(basepath + "include-json.json") as f:
        expected = json.load(f)
        # pprint.pprint(jsonExport["ResourceTemplate"])
        assert jsonExport["ResourceTemplate"] == expected["ResourceTemplate"]
        assert "examples" in jsonExport["DeploymentTemplate"], jsonExport[
            "DeploymentTemplate"
        ]
        # test environmentVariableNames export
        deploymentTemplate = jsonExport["DeploymentTemplate"]["examples"]
        assert deploymentTemplate["environmentVariableNames"] == [
            "APP_IMAGE",
            "APP_DOMAIN",
            "HOST_CPUS",
            "HOST_MEMORY",
            "HOST_STORAGE",
            "RESOLVER_CONSOLE_URL",
        ]
        resource = jsonExport["Resource"]["foo.com"]
        assert resource
        # test explicit "export" metadata
        assert resource["attributes"][0]["name"] == "console_url"
        # make sure console_url doesn't also appear here:
        assert not resource["computedProperties"]

    manifest = local.get_manifest()
    # verify included json was parsed correctly
    app_container = manifest.tosca.topology.node_templates.get("app_container")
    assert app_container and len(app_container.relationships) == 1, app_container and [
        r.source for r in app_container.relationships
    ]

    # assert app_container and len(app_container.instances) == 1, app_container and app_container.instances
    assert app_container.relationships[
        0
    ].source == manifest.tosca.topology.node_templates.get("the_app")

    # XXX verify that saving the manifest preserves the json include


@pytest.mark.parametrize("export_fn", [to_deployment, to_blueprint])
def test_jsonexport_requirement_visibility(export_fn):
    basepath = os.path.join(os.path.dirname(__file__), "examples/")
    local = LocalEnv(basepath + "visibility-metadata-ensemble.yaml")
    jsonExport = export_fn(local)
    assert jsonExport["ResourceTemplate"]["template1"]["dependencies"], pprint.pformat(
        jsonExport["ResourceTemplate"]["template1"]
    )
    assert (
        jsonExport["ResourceTemplate"]["template1"]["dependencies"][0]["constraint"][
            "visibility"
        ]
        == "visible"
    )
    app_type = jsonExport["ResourceType"]["App"]
    env_prop = jsonExport["ResourceType"]["ContainerHost"]["inputsSchema"]["properties"][
        "environment"
    ]
    # pprint.pprint(env_prop)
    assert env_prop == {
        "$toscatype": "SomeEnvVars",
        "additionalProperties": {"required": True, "type": "string"},
        # "bar": "default" is a user hidden property defined in SomeEnvVars
        # make sure that it is deleted from the default:
        "default": {"MARIADB_DATABASE": {"eval": "database_name"}, "foo": "foo"},
        "properties": {
            "$toscatype": {"const": "SomeEnvVars"},
            "foo": {
                "default": "foo",
                "required": True,
                "title": "foo",
                "type": "string",
                "user_settable": True,
            },
        },
        "tab_title": "Environment " "Variables",
        "title": "environment",
        "type": "object",
        "user_settable": True,
    }, pprint.pformat(env_prop)

    hostRequirement = app_type["requirements"][0]
    assert hostRequirement["inputsSchema"] == {
        "properties": {"image": None, "domain": {"default": "foo.com"}}
    }
    assert hostRequirement["requirementsFilter"] == [
        {
            "__typename": "RequirementConstraint",
            "name": "host",
            "description": "A compute instance with at least 2000MB RAM",
            "inputsSchema": {
                "properties": {
                    "Memory": {"minimum": 2000, "maximum": 20000},
                    "CPUs": {"default": 2},
                }
            },
            "resourceType": "ContainerHost",
        }
    ]
    assert jsonExport["ResourceTemplate"]["the_app"]["dependencies"], pprint.pformat(
        jsonExport["ResourceTemplate"]["the_app"]
    )
    assert (
        jsonExport["ResourceTemplate"]["the_app"]["dependencies"][0]["constraint"]
        == hostRequirement
    )


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
          defaults:
            implementation: foo
          Standard:
            operations:
              create:
              configure:
              delete:
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
              delete: not_implemented
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

        base:
          type: Base
          requirements:
          - host:
              node: the_app

"""
        manifest = YamlManifest(manifest)
        app_template = manifest.tosca.topology.node_templates["the_app"]
        req_def = (
            app_template.toscaEntityTemplate.type_definition.requirement_definitions[
                "host"
            ]
        )
        assert _find_implementation("Standard", "configure", app_template)

        assert req_def == {
            "metadata": {"base": "meta", "title": "derived host"},
            "relationship": {"type": "tosca.relationships.DependsOn"},
            "description": "A derived compute instance",
            "node": "Derived",
        }

        # make sure "defaults" doesn't mess up _find_implementation
        assert not _find_implementation("Standard", "start", app_template)
        assert not _find_implementation("Standard", "delete", app_template)
        base_template = manifest.tosca.topology.node_templates["base"]
        assert _find_implementation("Standard", "delete", base_template)


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
