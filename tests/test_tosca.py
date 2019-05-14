import unittest
from giterop.manifest import YamlManifest
from giterop.util import GitErOpError, GitErOpValidationError

manifest = '''
apiVersion: giterops/v1alpha1
kind: Manifest
root: {}
tosca:
  tosca_definitions_version: tosca_simple_yaml_1_0
  topology_template:
    node_templates:
      my_server:
        type: tosca.nodes.Compute
        capabilities:
          # Host container properties
          host:
           properties:
             num_cpus: 2
             disk_size: 10 GB
             mem_size: 512 MB
          # Guest Operating System properties
          os:
            properties:
              # host Operating System image properties
              architecture: x86_64
              type: Linux
              distribution: RHEL
              version: 6.5
        interfaces:
         Configurator:
          inputs:
            name: configuration1
            # provides: [capability1]
          instantiate:
            inputs:
              parameters:
              priority:
              parameterSchema:
              creates: [node-template1]
            implementation:
              # primary: ShellConfigurator # predefined python module artifact
              primary: # custom configurator
                file: scripts/pre_configure_source.py
                type: python.Configurator
                repository: my_service_catalog
              operation_host: HOST
              timeout: 120
'''

class ToscaSyntaxTest(unittest.TestCase):
  def test_hasversion(self):
    assert YamlManifest(manifest)
