import unittest
import os
from click.testing import CliRunner
import unfurl.manifest
from unfurl.yamlmanifest import YamlManifest
from unfurl.eval import Ref, map_value, RefContext

# expressions evaluate on tosca nodespecs (ignore validation errors)
# a compute instant that supports cloudinit and hosts a DockerComposeApp
# root __reflookup__ matches node templates by compatible type or template name
# nodes match relationships by requirement names
# relationships match source by compatible type or template name
class DecoratorTest(unittest.TestCase):
    def test_decorator(self):
        cliRunner = CliRunner()
        with cliRunner.isolated_filesystem():
            path = __file__ + "/../examples/decorators-ensemble.yaml"
            manifest = YamlManifest(path=path)

            ctx = RefContext(manifest.tosca.topology)
            result1 = Ref("my_server::dependency::tosca.nodes.Compute").resolve(ctx)
            self.assertEqual("my_server", result1[0].name)

            self.assertEqual(
                {"foo": "bar", "test": "annotated"},
                manifest.tosca.nodeTemplates["my_server"].properties,
            )
            for name in ["anode", "anothernode"]:
                node = manifest.tosca.nodeTemplates[name]
                self.assertEqual(
                    {"ports": [], "private_address": "annotated", 'imported': 'foo'}, node.properties
                )
