import unittest
import os
import unfurl.manifest
from unfurl.yamlmanifest import YamlManifest
from unfurl.localenv import LocalEnv
from unfurl.job import Runner, JobOptions
from unfurl.support import Status, _getbaseDir, RefContext
from unfurl.configurator import Configurator
from unfurl.util import sensitive_str, API_VERSION
from unfurl.yamlloader import makeVaultLib
import six
from click.testing import CliRunner

# python 2.7 needs these:
from unfurl.configurators.shell import ShellConfigurator


class SetAttributeConfigurator(Configurator):
    def run(self, task):
        from toscaparser.elements.portspectype import PortSpec

        if "ports" in task.inputs:
            ports = task.inputs["ports"]
            # target:source
            assert str(PortSpec(ports[0])) == "50000:9000", PortSpec(ports[0])
            assert str(PortSpec(ports[1])) == "20000-60000:1000-10000/udp", PortSpec(
                ports[1]
            )
            assert str(PortSpec(ports[2])) == "8000", PortSpec(ports[2])

        task.target.attributes["private_address"] = "10.0.0.1"
        yield task.done(True, Status.ok)


manifestDoc = """
apiVersion: unfurl/v1alpha1
kind: Manifest
context:
  inputs:
    cpus: 2
spec:
  service_template:
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
          TEST_VAR:
            type: unfurl.datatypes.EnvVar
          vars:
            type: map
            entry_schema:
              type: unfurl.datatypes.EnvVar
          access_token:
            type: tosca.datatypes.Credential
          event_object: # 5.3.2.2 Examples p.194
            type: tosca.datatypes.json
            constraints:
              - schema: >
                  {
                    "$schema": "http://json-schema.org/draft-04/schema#",
                    "description": "Example Event type schema",
                    "type": "object",
                    "properties": {
                      "uuid": {
                        "description": "The unique ID for the event.",
                        "type": "string"
                      }
                    }
                  }
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
          value: { get_attribute: [ testSensitive, private_address ] }
          # equivalent to {eval: "::testSensitive::private_address"}

      node_templates:
        testSensitive:
          type: testy.nodes.aNodeType
          requirements:
            - host: my_server
          properties:
            private_address: foo
            ports:
              - source: 9000
                target: 50000
              - target_range: [ 20000, 60000 ]
                source_range: [ 1000, 10000 ]
                protocol: udp
              - source: 8000
            TEST_VAR: foo
            vars:
              VAR1: more
            access_token:
              protocol: xauth
              token_type: X-Auth-Token
              token: 604bbe45ac7143a79e14f3158df67091
            event_object: >
              {
                 "uuid": "cadf:1234-56-0000-abcd"
              }
          interfaces:
           Standard:
            create:
              inputs:
                ports: "{{ SELF['ports'] }}"
              implementation:
                primary: SetAttribute
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
              implementation:
                primary: SetAttribute
                timeout: 120
"""


class ToscaSyntaxTest(unittest.TestCase):
    def _runInputAndOutputs(self, manifest):
        job = Runner(manifest).run(JobOptions(add=True, startTime="time-to-test"))
        assert not job.unexpectedAbort, job.unexpectedAbort.getStackTrace()
        my_server = manifest.getRootResource().findResource("my_server")
        assert my_server
        self.assertEqual(
            "10 GB", my_server.query({"get_property": ["SELF", "host", "disk_size"]})
        )
        assert my_server.attributes["test"], "cpus: 2"
        # print(job.out.getvalue())
        testSensitive = manifest.getRootResource().findResource("testSensitive")
        for name, toscaType in (
            ("access_token", "tosca.datatypes.Credential"),
            ("TEST_VAR", "unfurl.datatypes.EnvVar"),
        ):
            assert (
                testSensitive.template.attributeDefs[name].schema["type"] == toscaType
            )

        def t(datatype):
            return datatype.type == "unfurl.datatypes.EnvVar"

        envvars = set(testSensitive.template.findProps(testSensitive.attributes, t))
        self.assertEqual(envvars, set([("TEST_VAR", "foo"), ("VAR1", "more")]))
        outputIp = job.getOutputs()["server_ip"]
        self.assertEqual(outputIp, "10.0.0.1")
        assert isinstance(outputIp, sensitive_str), type(outputIp)
        assert job.status == Status.ok, job.summary()
        self.assertEqual("RHEL", testSensitive.attributes["distribution"])
        return outputIp, job

    def test_inputAndOutputs(self):
        manifest = YamlManifest(manifestDoc)
        outputIp, job = self._runInputAndOutputs(manifest)
        self.assertEqual(
            ["my_server", "testSensitive"],
            job.rootResource.query(
                "$nodes::.name",
                vars=dict(nodes={"get_nodes_of_type": "tosca.nodes.Root"}),
            ),
        )
        self.assertEqual(
            "testSensitive",
            job.rootResource.query({"get_nodes_of_type": "testy.nodes.aNodeType"})[
                0
            ].name,
        )
        assert not job.rootResource.query(
            {"get_nodes_of_type": "testy.nodes.Nonexistent"}
        )
        assert "server_ip: <<REDACTED>>" in job.out.getvalue(), job.out.getvalue()

    def test_ansibleVault(self):
        manifest = YamlManifest(manifestDoc, vault=makeVaultLib("a_password"))
        outputIp, job = self._runInputAndOutputs(manifest)
        vaultString = "server_ip: !vault |\n      $ANSIBLE_VAULT;1.1;AES256"
        assert vaultString in job.out.getvalue(), job.out.getvalue()

    def test_import(self):
        """
        Tests nested imports and url fragment resolution.
        """
        path = __file__ + "/../examples/testimport-manifest.yaml"
        manifest = YamlManifest(path=path)
        self.assertEqual(2, len(manifest.tosca.template.nested_tosca_tpls.keys()))

        runner = Runner(manifest)
        output = six.StringIO()
        job = runner.run(JobOptions(add=True, out=output, startTime="test"))
        self.assertEqual(job.status.name, "ok")
        self.assertEqual(job.stats()["ok"], 1)
        self.assertEqual(job.getOutputs()["aOutput"], "set")
        assert not job.unexpectedAbort, job.unexpectedAbort.getStackTrace()
        # print(output.getvalue())
        anInstance = job.rootResource.findResource("testPrefix")
        assert anInstance
        ctx = RefContext(anInstance)

        # .: <ensemble>/
        base = _getbaseDir(ctx, ".")
        self.assertEqual(base, os.path.normpath(os.path.dirname(path)))

        # testPrefix appeared in the same source file so it will be the same
        src = _getbaseDir(ctx, "src")
        self.assertEqual(src, base)

        # home: <ensemble>/<instance name>/
        home = _getbaseDir(ctx, "home")
        self.assertEqual(os.path.join(base, "testPrefix"), home)

        # local: <ensemble>/<instance name>/local/
        local = _getbaseDir(ctx, "local")
        self.assertEqual(os.path.join(home, "local"), local)

        tmp = _getbaseDir(ctx, "tmp")
        assert tmp.endswith("testPrefix"), tmp

        # spec.home: <spec>/<template name>/
        specHome = _getbaseDir(ctx, "spec.home")
        self.assertEqual(os.path.join(base, "spec", "testPrefix"), specHome)

        # spec.local: <spec>/<template name>/local/
        specLocal = _getbaseDir(ctx, "spec.local")
        self.assertEqual(os.path.join(specHome, "local"), specLocal)

        specSrc = _getbaseDir(ctx, "spec.src")
        self.assertEqual(src, specSrc)

        # these repositories should always be defined:
        unfurlRepoPath = _getbaseDir(ctx, "unfurl")
        self.assertEqual(unfurl.manifest._basepath, os.path.normpath(unfurlRepoPath))

        spec = _getbaseDir(ctx, "spec")
        self.assertEqual(os.path.normpath(spec), base)

        selfPath = _getbaseDir(ctx, "self")
        self.assertEqual(os.path.normpath(selfPath), base)

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
        # print(job.jsonSummary())
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
            properties:
              private_address:
                type: string
                required: false
                metadata:
                  sensitive: true
            interfaces:
               Install:
                operations:
                 check:
                   implementation: SetAttribute
      instances:
        anInstance:
         type: test.nodes.AbstractTest
    """
            % API_VERSION
        )

        localConfig = """
          apiVersion: unfurl/v1alpha1
          kind: Project
          contexts:
            defaults:
              secrets:
                attributes:
                  vault_default_password: a_password
              external:
               foreign:
                  manifest:
                    file:  foreignmanifest.yaml
                  instance: "*"  # this is the default
        """

        # import a node from a external manifest and have an abstract node template select it
        # check will be run on it each time
        mainManifest = (
            """
apiVersion: %s
kind: Manifest
spec:
  service_template:
    imports:
      - foreignmanifest.yaml#/spec/service_template
    topology_template:
      outputs:
        server_ip:
          value: {eval: "::foreign:anInstance::private_address"}
      node_templates:
        anInstance:
          type: test.nodes.AbstractTest
          directives:
             - select
  """
            % API_VERSION
        )

        runner = CliRunner()  # delete UNFURL_HOME
        try:
            UNFURL_HOME = os.environ.get("UNFURL_HOME")
            with runner.isolated_filesystem():
                os.environ["UNFURL_HOME"] = ""

                with open("foreignmanifest.yaml", "w") as f:
                    f.write(foreign)

                with open("unfurl.yaml", "w") as f:
                    f.write(localConfig)

                with open("manifest.yaml", "w") as f:
                    f.write(mainManifest)

                manifest = LocalEnv("manifest.yaml").getManifest()
                assert manifest.manifest.vault and manifest.manifest.vault.secrets
                job = Runner(manifest).run(
                    JobOptions(add=True, startTime="time-to-test")
                )
                # print(job.out.getvalue())
                # print(job.jsonSummary(True))
                assert job.status == Status.ok, job.summary()
                self.assertEqual(
                    [
                        {
                            "operation": "check",
                            "configurator": "test_tosca.SetAttributeConfigurator",
                            "changed": True,
                            "priority": "required",
                            "reason": "check",
                            "status": "ok",
                            "target": "foreign:anInstance",
                            "targetStatus": "ok",
                            "template": "anInstance",
                            "type": "test.nodes.AbstractTest",
                        }
                    ],
                    job.jsonSummary()["tasks"],
                )
                job.getOutputs()
                self.assertEqual(job.getOutputs()["server_ip"], "10.0.0.1")
                self.assertEqual(
                    len(manifest.localEnv._manifests), 2, manifest.localEnv._manifests
                )
                # print("output", job.out.getvalue())
                assert "10.0.0.1" not in job.out.getvalue(), job.out.getvalue()
                vaultString1 = "server_ip: !vault |\n      $ANSIBLE_VAULT;1.1;AES256"
                assert vaultString1 in job.out.getvalue()
                vaultString2 = (
                    "private_address: !vault |\n          $ANSIBLE_VAULT;1.1;AES256"
                )
                assert vaultString2 in job.out.getvalue()

                # reload:
                manifest2 = LocalEnv("manifest.yaml").getManifest()
                assert manifest2.lastJob
                # test that restored manifest create a shadow instance for the foreign instance
                imported = manifest2.imports["foreign"].resource
                assert imported
                imported2 = manifest2.imports.findImport("foreign:anInstance")
                assert imported2
                assert imported2.shadow
                self.assertIs(imported2.root, manifest2.getRootResource())
                self.assertEqual(imported2.attributes["private_address"], "10.0.0.1")
                self.assertIsNot(imported2.shadow.root, manifest2.getRootResource())
        finally:
            if UNFURL_HOME is not None:
                os.environ["UNFURL_HOME"] = UNFURL_HOME

    def test_connections(self):
        mainManifest = (
            """
apiVersion: %s
kind: Manifest
spec:
  service_template:
    topology_template:
      node_templates:
        defaultCluster:
          directives:
            - default
          type: unfurl.nodes.K8sCluster
        myCluster:
          type: unfurl.nodes.K8sCluster
        localhost:
          type: unfurl.nodes.Default
          requirements:
            - connect: # section 3.8.2 p140
                relationship:
                  # think of this as a connection, "discover" will figure out what's on the other end
                  type: unfurl.relationships.ConnectsTo.K8sCluster
                  properties:
                    context: docker-desktop
  """
            % API_VERSION
        )
        manifest2 = YamlManifest(mainManifest)
        nodeSpec = manifest2.tosca.getTemplate("localhost")
        assert nodeSpec
        relationshipSpec = nodeSpec.requirements["connect"].relationship
        assert relationshipSpec
        self.assertEqual(relationshipSpec.name, "connect")
        self.assertEqual(
            relationshipSpec.type, "unfurl.relationships.ConnectsTo.K8sCluster"
        )
        # chooses myCluster instead of the cluster with the "default" directive
        assert relationshipSpec.target.name == "myCluster"
