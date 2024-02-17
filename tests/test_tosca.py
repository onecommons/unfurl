import logging
import unittest
import os

import pytest
from unfurl.graphql import ImportDef, get_local_type
import unfurl.manifest
from unfurl.yamlmanifest import YamlManifest
from unfurl.localenv import LocalEnv
from unfurl.job import Runner, JobOptions
from unfurl.support import Status, RefContext
from unfurl.projectpaths import _get_base_dir
from unfurl.configurator import Configurator
from unfurl.util import sensitive_str, API_VERSION, UnfurlValidationError
from unfurl.yamlloader import make_vault_lib
from unfurl.spec import find_env_vars
import io
from click.testing import CliRunner
from toscaparser.topology_template import find_type


class SetAttributeConfigurator(Configurator):
    def run(self, task):
        from toscaparser.elements.portspectype import PortSpec

        if "ports" in task.inputs:
            ports = task.inputs["ports"]
            # target:source
            assert PortSpec(ports[0]).spec == "50000:9000", PortSpec(ports[0]).spec
            assert PortSpec(ports[1]).spec == "20000-60000:1000-10000/udp", PortSpec(
                ports[1]
            ).spec
            assert PortSpec(ports[2]).spec == "8000", PortSpec(ports[2]).spec
        task.target.attributes["private_address"] = "10.0.0.1"
        yield task.done(True, Status.ok)


_manifestDoc = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
environment:
  inputs: %s
spec:
  service_template:
    node_types:
      testy.nodes.aNodeType:
        derived_from: tosca.nodes.Root
        requirements:
          - host:
              capability: tosca.capabilities.Compute
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
              env_vars:
                - ADDRESS
                - PRIVATE_ADDRESS
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
          ip_addresses:
            type: map
            default:
              private:
                  eval: private_address
            
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
        server_ip_again:        
          value: {eval: "::testSensitive::ip_addresses::private"}

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
            concat2:
              eval:
                concat:
                  eval: ::empty
                sep: ","
          capabilities:
            # Host container properties
            host:
             properties:
               num_cpus: { eval: ::root::inputs::cpus }
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

manifestDoc = _manifestDoc % "{cpus: 2}"
missingInputsDoc = _manifestDoc % "{}"


class ToscaSyntaxTest(unittest.TestCase):
    def setUp(self):
        self.maxDiff = None

    def _runInputAndOutputs(self, manifest):
        job = Runner(manifest).run(JobOptions(add=True, startTime="time-to-test"))
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        my_server = manifest.get_root_resource().find_resource("my_server")
        assert my_server
        self.assertEqual(
            "10 GB", my_server.query({"get_property": ["SELF", "host", "disk_size"]})
        )
        assert my_server.attributes["test"] == "cpus: 2"
        assert my_server.attributes["concat2"] is None
        # print(job.out.getvalue())
        testSensitive = manifest.get_root_resource().find_resource("testSensitive")
        for name, toscaType in (
            ("access_token", "tosca.datatypes.Credential"),
            ("TEST_VAR", "unfurl.datatypes.EnvVar"),
        ):
            assert testSensitive.template.propertyDefs[name].schema["type"] == toscaType

        envvars = set(
            find_env_vars(testSensitive.template.find_props(testSensitive.attributes))
        )
        self.assertEqual(
            envvars,
            set(
                [
                    ("TEST_VAR", "foo"),
                    ("VAR1", "more"),
                    ("ADDRESS", "10.0.0.1"),
                    ("PRIVATE_ADDRESS", "10.0.0.1"),
                ]
            ),
        )
        outputIp = job.get_outputs()["server_ip"]
        self.assertEqual(outputIp, "10.0.0.1")
        assert isinstance(outputIp, sensitive_str), type(outputIp)
        assert job.status == Status.ok, job.summary()
        self.assertEqual("RHEL", testSensitive.attributes["distribution"])
        return outputIp, job

    def test_inputAndOutputs(self):
        with self.assertRaises(UnfurlValidationError) as err:
            manifest = YamlManifest(missingInputsDoc)

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
        manifest = YamlManifest(manifestDoc, vault=make_vault_lib("a_password"))
        outputIp, job = self._runInputAndOutputs(manifest)
        vaultString = "server_ip: !vault |\n      $ANSIBLE_VAULT;1.1;AES256"
        assert vaultString in job.out.getvalue(), job.out.getvalue()
        vaultString2 = "server_ip_again: !vault |\n      $ANSIBLE_VAULT;1.1;AES256"
        assert vaultString2 in job.out.getvalue(), job.out.getvalue()

        vaultString3 = "private: !vault |"
        assert vaultString3 in job.out.getvalue(), job.out.getvalue()

        from unfurl.yamlloader import cleartext_yaml

        manifest = YamlManifest(manifestDoc, vault=cleartext_yaml.representer.vault)
        outputIp, job = self._runInputAndOutputs(manifest)
        assert "!vault" not in job.out.getvalue(), job.out.getvalue()

    def test_import(self):
        """
        Tests nested imports and url fragment resolution.
        """
        path = __file__ + "/../examples/testimport-ensemble.yaml"
        local_env = LocalEnv(path)
        manifest = local_env.get_manifest()

        self.assertEqual(
            2,
            len(manifest.tosca.template.nested_tosca_tpls),
            list(manifest.tosca.template.nested_tosca_tpls),
        )
        assert "imported-repo" in manifest.tosca.template.repositories
        assert "nested-imported-repo" in manifest.tosca.template.repositories, [
            tosca_tpl.get("repositories")
            for tosca_tpl, namespace_id in manifest.tosca.template.nested_tosca_tpls.values()
        ]
        assert ["A.Nested", "A.nodes.types", "A.Test"] == list(
            manifest.tosca.template.topology_template.custom_defs
        )

        runner = Runner(manifest)
        output = io.StringIO()
        job = runner.run(JobOptions(add=True, out=output, startTime="test"))
        self.assertEqual(job.status.name, "ok")
        self.assertEqual(job.stats()["ok"], 1)
        self.assertEqual(job.get_outputs()["aOutput"], "set")
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        # print(output.getvalue())
        anInstance = job.rootResource.find_resource("testPrefix")
        assert anInstance
        self.assertEqual(anInstance.attributes["testExpressionFunc"], "foo")
        self.assertEqual(anInstance.attributes["defaultexpession"], "default_foo")

        # base_dir should be the directory the template's type is defined in
        base_dir = os.path.normpath(os.path.join(os.path.dirname(path), "spec"))
        assert (
            anInstance.template.propertyDefs["defaultexpession"].value.base_dir
            == base_dir
        )
        assert (
            anInstance.template.toscaEntityTemplate.type_definition.defs.base_dir
            == base_dir
        )

        instance2 = job.rootResource.find_resource("testNested")
        assert instance2
        assert instance2.template.propertyDefs["a_property"].value.base_dir == base_dir
        assert (
            instance2.template.toscaEntityTemplate.type_definition.defs.base_dir
            == base_dir
        )

        ctx = RefContext(anInstance)

        # .: <ensemble>/
        base = _get_base_dir(ctx, ".")
        self.assertEqual(base, os.path.normpath(os.path.dirname(path)))

        # testPrefix appeared in the same source file so it will be the same
        src = _get_base_dir(ctx, "src")
        self.assertEqual(src, base)

        # home: <ensemble>/artifacts/<instance name>
        home = _get_base_dir(ctx, "artifacts")
        self.assertEqual(os.path.join(base, "artifacts", "testPrefix"), home)

        # local: <ensemble>/local/<instance name>
        local = _get_base_dir(ctx, "local")
        self.assertEqual(os.path.join(base, "local", "testPrefix"), local)

        tmp = _get_base_dir(ctx, "tmp")
        assert tmp.endswith("testPrefix"), tmp

        # spec.home: <spec>/<template name>/
        specHome = _get_base_dir(ctx, "spec.home")
        self.assertEqual(os.path.join(base, "spec", "testPrefix"), specHome)

        # spec.local: <spec>/<template name>/local/
        specLocal = _get_base_dir(ctx, "spec.local")
        self.assertEqual(os.path.join(specHome, "local"), specLocal)

        specSrc = _get_base_dir(ctx, "spec.src")
        self.assertEqual(src, specSrc)

        # these repositories should always be defined:
        unfurlRepoPath = _get_base_dir(ctx, "unfurl")
        self.assertEqual(unfurl.manifest._basepath, os.path.normpath(unfurlRepoPath))

        spec = _get_base_dir(ctx, "spec")
        self.assertEqual(os.path.normpath(spec), base)

        selfPath = _get_base_dir(ctx, "self")
        self.assertEqual(os.path.normpath(selfPath), base)

        repoPath = _get_base_dir(ctx, "nested-imported-repo")
        self.assertEqual(
            os.path.normpath(repoPath),
            base,
            f"{repoPath} vs {base} vs {os.path.abspath('./')}",
        )

    @unittest.skipIf("k8s" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
    def test_workflows(self):
        os.environ["UNFURL_WORKDIR"] = os.environ["UNFURL_TMPDIR"]
        manifest = YamlManifest(
            path=__file__ + "/../examples/test-workflow-ensemble.yaml"
        )
        # print(manifest.tosca.template.nested_tosca_tpls)
        self.assertEqual(len(manifest.tosca._workflows), 3)

        runner = Runner(manifest)
        output = io.StringIO()
        job = runner.run(
            JobOptions(
                add=True, check=True, planOnly=False, out=output, startTime="test"
            )
        )
        del os.environ["UNFURL_WORKDIR"]
        # print(json.dumps(job.json_summary(), indent=2))
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        self.assertEqual(job.status.name, "ok")
        self.assertEqual(job.stats()["ok"], 4)
        self.assertEqual(job.stats()["changed"], 4)
        # print(job._json_plan_summary(True))
        plan_summary = job._json_plan_summary(include_rendered=False)
        assert (
            plan_summary[0]["instance"]
            == "__artifact__configurator-artifacts--kubernetes.core"
        )
        plan_summary.pop(0)
        self.assertEqual(
            plan_summary,
            [
                {
                    "instance": "stagingCluster",
                    "status": "Status.ok",
                    "state": "NodeState.started",
                    "managed": None,
                    "plan": [{"operation": "check", "reason": "check"}],
                },
                {
                    "instance": "defaultNamespace",
                    "status": "Status.ok",
                    "state": "NodeState.started",
                    "managed": False,  # set to false because "default" namespace already exists
                    "plan": [
                        {"operation": "check", "reason": "check"},
                        {
                            "workflow": "deploy",
                            "sequence": [{"operation": "configure", "reason": "add"}],
                        },
                    ],
                },
                {
                    "instance": "gitlab-release",
                    "status": "Status.unknown",  # XXX used to be "Status.ok"
                    "state": "None",
                    "managed": None,  # XXX "A01100000003",
                    "plan": [
                        {
                            "workflow": "deploy",
                            "sequence": [
                                {
                                    "workflow": "deploy",
                                    "sequence": [
                                        {"operation": "execute", "reason": "step:helm"},
                                        {
                                            "workflow": "discover",
                                            "sequence": [
                                                {
                                                    "operation": "discover",
                                                    "reason": "step:helm",
                                                }
                                            ],
                                        },
                                    ],
                                }
                            ],
                        }
                    ],
                },
            ],
        )

    def test_missing_type_is_handled_by_unfurl(self):
        ensemble = """
        apiVersion: unfurl/v1alpha1
        kind: Ensemble
        configurations:
          create:
            implementation:
              className: unfurl.configurators.shell.ShellConfigurator
            inputs:
              command: echo hello
        spec:
          service_template:
            topology_template:
              node_templates:
                test_node:
                  # type: tosca.nodes.Root
                  interfaces:
                    Standard:
                      +/configurations:
        """
        with self.assertRaises(UnfurlValidationError) as err:
            YamlManifest(ensemble)

        assert (
            'MissingRequiredFieldError: Template "test_node" is missing required field "type"'
            in str(err.exception)
        )

    def test_missing_interface_definition_is_handled_by_unfurl(self):
        ensemble = """
        apiVersion: unfurl/v1alpha1
        kind: Ensemble
        spec:
          service_template:
            topology_template:
              node_templates:
                test_node:
                  type: tosca.nodes.Root
                  interfaces:
                    Standard:
                      badop: # missing missing implementation - raises error:
        """
        with self.assertRaises(UnfurlValidationError) as err:
            YamlManifest(ensemble)

        assert "UnknownFieldError" in str(err.exception)


class AbstractTemplateTest(unittest.TestCase):
    def test_import(self):
        foreign = (
            """
    apiVersion: %s
    kind: Ensemble
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
                 connect:
                   implementation: SetAttribute
      instances:
        anInstance:
          template:
            type: test.nodes.AbstractTest
    """
            % API_VERSION
        )

        localConfig = """
          apiVersion: unfurl/v1alpha1
          kind: Project
          environments:
            defaults:
              repositories:
                in_context:
                  url: file:.
              secrets:
                  vault_secrets:
                    default: a_password
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
kind: Ensemble
spec:
  service_template:
    imports:
      - file: foreignmanifest.yaml#/spec/service_template
        repository: in_context
    topology_template:
      outputs:
        server_ip:
          value: {eval: "::anInstance::private_address"}
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

                manifest = LocalEnv("manifest.yaml").get_manifest()
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
                            "operation": "connect",
                            "configurator": "tests.test_tosca.SetAttributeConfigurator",
                            "changed": True,
                            "priority": "required",
                            "reason": "connect",
                            "status": "ok",
                            "target": "anInstance",
                            "targetStatus": "ok",
                            "targetState": None,  # "started",
                            "template": "anInstance",
                            "type": "test.nodes.AbstractTest",
                        }
                    ],
                    job.json_summary()["tasks"],
                )
                job.get_outputs()
                self.assertEqual(job.get_outputs()["server_ip"], "10.0.0.1")
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
                # modifications to imported instances are not saved:
                assert vaultString2 not in job.out.getvalue()

                # reload:
                manifest2 = LocalEnv("manifest.yaml").get_manifest()
                assert manifest2.lastJob
                # test that restored manifest create a shadow instance for the foreign instance
                imported = manifest2.imports["foreign"]
                assert imported
                imported2 = manifest2.imports.find_import("foreign:anInstance")
                assert imported2
                assert imported2.imported == "foreign:anInstance"
                assert imported2.shadow
                self.assertIs(imported2.root, manifest2.get_root_resource())
                # modifications to imported instances are not saved:
                assert "private_address" not in imported2.attributes
                self.assertIsNot(imported2.shadow.root, manifest2.get_root_resource())
        finally:
            if UNFURL_HOME is not None:
                os.environ["UNFURL_HOME"] = UNFURL_HOME

    def test_connections(self):
        mainManifest = (
            """
apiVersion: %s
kind: Ensemble
spec:
  service_template:
    imports:
      - repository: unfurl
        file: tosca_plugins/k8s.yaml
    topology_template:
      node_templates:
        myCluster:
          type: unfurl.nodes.K8sCluster
        defaultCluster:
          directives:
            - default
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
        nodeSpec = manifest2.tosca.get_template("localhost")
        assert nodeSpec
        relationshipSpec = nodeSpec.requirements["connect"].relationship
        assert relationshipSpec
        self.assertEqual(relationshipSpec.name, "connect")
        self.assertEqual(
            relationshipSpec.type, "unfurl.relationships.ConnectsTo.K8sCluster"
        )
        # chooses myCluster instead of the cluster with the "default" directive
        assert relationshipSpec.target.name == "myCluster"


prefixed_k8s_manifest = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
spec:
  service_template:
    imports:
      - repository: unfurl
        file: tosca_plugins/k8s.yaml
        namespace_prefix: k8s
      - repository: unfurl
        file: configurators/templates/dns.yaml
        namespace_prefix: dns

    relationship_types:
        MyHostedOn:
            derived_from: tosca.relationships.HostedOn

    node_types:
      MyNamespace:
        # test unfurl.nodes._K8sResourceHost resolves
        derived_from: k8s.unfurl.nodes.K8sNamespace
        requirements:
          - host:
              relationship: MyHostedOn
        # capabilities
        interfaces:
          Install:
            operations:
              check:
                implementation: kubectl

      MyZone:
        derived_from: dns.unfurl.nodes.DNSZone
        properties:
          records:
            description: make sure overloaded properties resolve datatypes correctly
            default:
              record1:
                 type: TXT
            metadata:
              my_metadata: test

    topology_template:
      node_templates:
        myNamespace:
          type: MyNamespace

        myResource:
          type: %sunfurl.nodes.K8sResource
          requirements:
          - host: myNamespace

        myDNS:
          type: MyZone
          properties:
            name: test
            provider: {}
            records:
              t1:
                type: A
                value: 10.10.10.1
      """


def test_namespaces(caplog):
    with caplog.at_level(logging.DEBUG):
        manifest = YamlManifest(prefixed_k8s_manifest % "k8s.", "fakefile.yaml")
        assert manifest
        assert 'No definition for type "MyHostedOn" found' not in caplog.text

    namespace = manifest.tosca.topology.topology_template.custom_defs
    mock_importdef = ImportDef(file="", url="https://foo.com", prefix="dns")
    assert ("dns.unfurl.nodes.DNSZone", None) == get_local_type(
        namespace,
        "unfurl.nodes.DNSZone@unfurl:configurators/templates/dns",
        mock_importdef,
    )
    # avoids prefix clash:
    assert ("dns1.UnknownType", mock_importdef) == get_local_type(
        namespace, "UnknownType@foo.com", mock_importdef
    )
    assert mock_importdef["prefix"] == "dns1"
    # generate prefix:
    mock_importdef.pop("prefix")
    assert ("foo_com.UnknownType", mock_importdef) == get_local_type(
        namespace, "UnknownType@foo.com", mock_importdef
    )
    assert mock_importdef["prefix"] == "foo_com"

    MyZone = find_type("MyZone", namespace)
    # verify !namespace attributes
    assert MyZone.requirements == [
        {
            "parent_zone": {
                "metadata": {"visibility": "hidden"},
                "capability": "unfurl.capabilities.DNSZone",
                "occurrences": [0, 1],
                "!namespace-capability": "unfurl:configurators/templates/dns",
            }
        },
        {
            "dependency": {
                "capability": "tosca.capabilities.Node",
                "node": "tosca.nodes.Root",
                "relationship": "tosca.relationships.DependsOn",
                "occurrences": [0, "UNBOUNDED"],
            }
        },
    ]

    with pytest.raises(UnfurlValidationError) as err:
        YamlManifest(prefixed_k8s_manifest % "")  # no prefix
