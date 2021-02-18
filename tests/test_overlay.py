import unittest
import os
import unfurl.manifest
from unfurl.yamlmanifest import YamlManifest
from unfurl.eval import Ref, mapValue, RefContext

manifestDoc = """
apiVersion: unfurl/v1alpha1
kind: Manifest
spec:
  service_template:
    decorators:
      missing:
        properties:
          test: missing

      my_server::dependency::tosca.nodes.Compute:
        properties:
          test: annotated

      testy.nodes.aNodeType:
        properties:
          private_address: "annotated"
          ports: []

    node_types:
      testy.nodes.aNodeType:
        derived_from: tosca.nodes.Root
        requirements:
          - host:
              capabilities: tosca.capabilities.Compute
              relationship: tosca.relationships.HostedOn
        attributes:
          distribution:
            type: string
            default: { get_attribute: [ HOST, os, distribution ] }
        properties:
          private_address:
            type: string
            metadata:
              sensitive: true
          ports:
            type: list
            entry_schema:
              type: tosca.datatypes.network.PortSpec

    topology_template:
      node_templates:
        anode:
          type: testy.nodes.aNodeType
          # this is in error without the annotations: missing properties

        anothernode:
          type: testy.nodes.aNodeType
          properties:
            private_address: "base"
            ports: []

        my_server:
          type: tosca.nodes.Compute
          capabilities:
            # Host container properties
            host:
             properties:
               num_cpus: { eval: ::inputs::cpus }
               disk_size: 10 GB
               mem_size: 512 MB

          properties:
            foo: bar
          requirements:
            - dependency: my_server
"""

# expressions evaluate on tosca nodespecs (ignore validation errors)
# a compute instant that supports cloudinit and hosts a DockerComposeApp
# root __reflookup__ matches node templates by compatible type or template name
# nodes match relationships by requirement names
# relationships match source by compatible type or template name
class OverlayTest(unittest.TestCase):
    def test_inputAndOutputs(self):
        manifest = YamlManifest(manifestDoc)

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
                {"ports": [], "private_address": "annotated"}, node.properties
            )
