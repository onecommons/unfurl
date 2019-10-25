import unittest
from unfurl.yamlmanifest import YamlManifest
from unfurl.job import Runner, JobOptions
from unfurl.support import Status
from unfurl.configurator import Configurator
# from unfurl.util import UnfurlError, UnfurlValidationError

class SetAttributeConfigurator(Configurator):
  def run(self, task):
    task.target.attributes['private_address'] = '10.0.0.1'
    yield task.createResult(True, True, Status.ok)

manifestDoc = '''
apiVersion: unfurl/v1alpha1
kind: Manifest
spec:
  inputs:
    cpus: 2
  tosca:
    node_types:
      testy.nodes.aNodeType:
        derived_from: tosca.nodes.Root
        properties:
          private_address:
            type: string
            metadata:
              sensitive: true
    topology_template:
      inputs:
        cpus:
          type: integer
          description: Number of CPUs for the server.
          constraints:
            - valid_values: [ 1, 2, 4, 8 ]
          metadata:
            sensitive: true
      outputs:
        server_ip:
          description: The private IP address of the provisioned server.
          # equivalent to { get_attribute: [ my_server, private_address ] }
          value: {eval: "::my_server::private_address"}
      node_templates:
        testSensitive:
          type: testy.nodes.aNodeType
          properties:
            private_address: foo
          interfaces:
           Standard:
            create:
              implementation:
                primary: SetAttributeConfigurator
        my_server:
          type: tosca.nodes.Compute
          properties:
            test:  { concat: ['cpus: ', {get_input: cpus }] }
          capabilities:
            # Host container properties
            host:
             properties:
               num_cpus: { eval: ::inputs::cpus }
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
           Standard:
            create:
              inputs:
                inputs:
                priority:
                inputSchema:
              implementation:
                primary: SetAttributeConfigurator
                timeout: 120
'''

class ToscaSyntaxTest(unittest.TestCase):
  def test_inputAndOutputs(self):
    manifest = YamlManifest(manifestDoc)
    job = Runner(manifest).run(JobOptions(add=True, startTime="time-to-test"))
    assert not job.unexpectedAbort, job.unexpectedAbort.getStackTrace()
    assert manifest.getRootResource().findResource('my_server').attributes['test'], 'cpus: 2'
    assert job.getOutputs()['server_ip'], '10.0.0.1'
    assert job.status == Status.ok, job.summary()
    # XXX verify redacted output
    # print(job.out.getvalue())
