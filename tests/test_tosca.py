import unittest
from unfurl.yamlmanifest import YamlManifest
from unfurl.localenv import LocalEnv
from unfurl.job import Runner, JobOptions
from unfurl.support import Status
from unfurl.configurator import Configurator
from unfurl.util import sensitive_str, VERSION
import six
from click.testing import CliRunner


class SetAttributeConfigurator(Configurator):
    def run(self, task):
        task.target.attributes["private_address"] = "10.0.0.1"
        yield task.done(True, Status.ok)


manifestDoc = """
apiVersion: unfurl/v1alpha1
kind: Manifest
spec:
  inputs:
    cpus: 2
  service_template:
    node_types:
      testy.nodes.aNodeType:
        derived_from: tosca.nodes.Root
        properties:
          private_address:
            type: string
            metadata:
              sensitive: true
          TEST_VAR:
            type: unfurl.datatypes.EnvVar
          vars:
            type: map
            entry_schema:
              type: unfurl.datatypes.EnvVar
          access_token:
            type: tosca.datatypes.Credential
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
          value: {eval: "::testSensitive::private_address"}
      node_templates:
        testSensitive:
          type: testy.nodes.aNodeType
          properties:
            private_address: foo
            TEST_VAR: foo
            vars:
              VAR1: more
            access_token:
              protocol: xauth
              token_type: X-Auth-Token
              token: 604bbe45ac7143a79e14f3158df67091
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
              implementation:
                primary: SetAttributeConfigurator
                timeout: 120
"""


class ToscaSyntaxTest(unittest.TestCase):
    def test_inputAndOutputs(self):
        manifest = YamlManifest(manifestDoc)
        job = Runner(manifest).run(JobOptions(add=True, startTime="time-to-test"))
        assert not job.unexpectedAbort, job.unexpectedAbort.getStackTrace()
        my_server = manifest.getRootResource().findResource("my_server")
        assert my_server.attributes["test"], "cpus: 2"
        # print(job.out.getvalue())
        testSensitive = manifest.getRootResource().findResource("testSensitive")
        for name, type in (
            ("access_token", "tosca.datatypes.Credential"),
            ("TEST_VAR", "unfurl.datatypes.EnvVar"),
        ):
            assert testSensitive.template.attributeDefs[name].schema["type"] == type

        def t(datatype):
            return datatype.type == "unfurl.datatypes.EnvVar"

        envvars = set(testSensitive.template.findProps(testSensitive.attributes, t))
        self.assertEqual(envvars, set([("TEST_VAR", "foo"), ("VAR1", "more")]))
        outputIp = job.getOutputs()["server_ip"]
        assert outputIp, "10.0.0.1"
        assert isinstance(outputIp, sensitive_str)
        assert "server_ip: <<REDACTED>>" in job.out.getvalue()
        assert job.status == Status.ok, job.summary()

    def test_import(self):
        """
      Tests nested imports and url fragment resolution.
      """
        manifest = YamlManifest(path=__file__ + "/../examples/testimport-manifest.yaml")
        self.assertEqual(2, len(manifest.tosca.template.nested_tosca_tpls.keys()))

        runner = Runner(manifest)
        output = six.StringIO()
        job = runner.run(JobOptions(add=True, out=output, startTime="test"))
        self.assertEqual(job.status.name, "ok")
        self.assertEqual(job.stats()["ok"], 1)
        self.assertEqual(job.getOutputs()["aOutput"], "set")
        assert not job.unexpectedAbort, job.unexpectedAbort.getStackTrace()

    def test_workflows(self):
        manifest = YamlManifest(
            path=__file__ + "/../examples/test-workflow-manifest.yaml"
        )
        # print(manifest.tosca.template.nested_tosca_tpls)
        self.assertEqual(len(manifest.tosca._workflows), 3)

        runner = Runner(manifest)
        output = six.StringIO()
        job = runner.run(
            JobOptions(add=True, planOnly=True, out=output, startTime="test")
        )
        # print(job.summary())
        assert not job.unexpectedAbort, job.unexpectedAbort.getStackTrace()
        self.assertEqual(job.status.name, "ok")
        self.assertEqual(job.stats()["ok"], 4)
        self.assertEqual(job.stats()["changed"], 4)


class AbstractTemplateTest(unittest.TestCase):
    def test_import(self):
        foreign = (
            """
    apiVersion: %s
    kind: Manifest
    spec:
      service_template:
        node_types:
          test.nodes.AbstractTest:
            derived_from: tosca.nodes.Root
            interfaces:
               Install:
                 check:
                   implementation: SetAttributeConfigurator
      instances:
        anInstance:
         type: test.nodes.AbstractTest
    """
            % VERSION
        )

        # import a node from a external manifest and have an abstract node template select it
        # check will be run on it each time
        mainManifest = (
            """
apiVersion: %s
kind: Manifest
imports:
 foreign:
    file:  foreignmanifest.yaml
    instance: "*"  # this is the default
spec:
  service_template:
    imports:
      - foreignmanifest.yaml#/spec/service_template
    topology_template:
      outputs:
        server_ip:
          value: {eval: "::foreign:anInstance::private_address"}
      node_templates:
        abstract:
          type: test.nodes.AbstractTest
          directives:
             - select
  """
            % VERSION
        )

        runner = CliRunner()
        with runner.isolated_filesystem():
            with open("foreignmanifest.yaml", "w") as f:
                f.write(foreign)

            with open("manifest.yaml", "w") as f:
                f.write(mainManifest)

            manifest = YamlManifest(localEnv=LocalEnv("manifest.yaml"))
            job = Runner(manifest).run(JobOptions(add=True, startTime="time-to-test"))
            # print(job.out.getvalue())
            # print(job.jsonSummary())
            assert job.status == Status.ok, job.summary()
            self.assertEqual(
                job.jsonSummary()["tasks"],
                [["foreign:anInstance:Install.check:check:1", "ok"]],
            )
            self.assertEqual(job.getOutputs()["server_ip"], "10.0.0.1")

            # reload
            manifest2 = YamlManifest(localEnv=LocalEnv("manifest.yaml"))
            # test that restored manifest create a shadow instance for the foreign instance
            imported = manifest2.imports["foreign"].resource
            assert imported
            imported2 = manifest2.imports.findImport("foreign:anInstance")
            assert imported2
            assert imported2.shadow
            self.assertIs(imported2.root, manifest2.getRootResource())
            self.assertEqual(imported2.attributes["private_address"], "10.0.0.1")
            self.assertIsNot(imported2.shadow.root, manifest2.getRootResource())
