import copy
import logging
import os.path
import pickle
import unittest

import io
from click.testing import CliRunner
import pytest
from ruamel.yaml.comments import CommentedMap

from unfurl.configurator import Configurator, ConfigurationSpec
from unfurl.job import JobOptions, Runner
from unfurl.localenv import LocalEnv
from unfurl.merge import (
    expand_doc,
    restore_includes,
    diff_dicts,
    merge_dicts,
    patch_dict,
    parse_merge_key,
)
from unfurl.runtime import Status, Priority, NodeInstance, OperationalInstance
from unfurl.support import NodeState
from unfurl.util import (
    UnfurlError,
    UnfurlValidationError,
    lookup_class,
    API_VERSION,
    sensitive_str,
    should_include_path
)
from unfurl.yamlloader import YamlConfig
from unfurl.yamlmanifest import YamlManifest
from unfurl import logs

class SimpleConfigurator(Configurator):
    def run(self, task):
        assert self.can_run(task)
        return Status.ok


simpleConfigSpec = ConfigurationSpec("subtask", "instantiate", "Simple", 0)


class TestSubtaskConfigurator(Configurator):
    def run(self, task):
        assert self.can_run(task)
        configuration = yield task.create_sub_task(simpleConfigSpec)
        assert configuration.status == Status.ok, configuration.status
        # print ("running TestSubtaskConfigurator")
        yield task.done(True, Status.ok)


class ExpandDocTest(unittest.TestCase):
    doc = {
        "t1": {"b": 2},
        "t2": {"a": {"b": 1}, "c": "c"},
        "t3": "val",
        "t4": ["a", "b"],
        "test1": CommentedMap(
            [("+/t2", None), ("a", {"+/t1": None}), ("d", {"+/t3": None}), ("e", "e")]
        ),
        "test2": [1, {"+/t4": ""}, "+t4", {"+/t4": None}],
        "base": {"list": [1]},
        "test3": {"list": [2, 1, 3], "+/base": None},
        "test4": {"+/t2": None, "a": None},
    }

    expected = {
        "test1": {"a": {"b": 2}, "c": "c", "d": "val", "e": "e"},
        "test2": [1, "a", "b", "+t4", "a", "b"],
        "test3": {"list": [1, 2, 3]},
        # "a" merged even though it was originally None
        "test4": {"a": {"b": 1}, "c": "c"},
    }

    def test_expandDoc(self):
        includes, expanded = expand_doc(self.doc, cls=CommentedMap)
        self.assertEqual(
            includes,
            {
                ("test1",): [(parse_merge_key("+/t2"), None)],
                ("test1", "a"): [(parse_merge_key("+/t1"), None)],
                ("test1", "d"): [(parse_merge_key("+/t3"), None)],
                ("test2", 1): [(parse_merge_key("+/t4"), "")],
                ("test2", 3): [(parse_merge_key("+/t4"), None)],
                ("test3",): [(parse_merge_key("+/base"), None)],
                ("test4",): [(parse_merge_key("+/t2"), None)],
            },
        )
        self.assertEqual(expanded["test1"], self.expected["test1"])
        self.assertEqual(expanded["test2"], self.expected["test2"])
        self.assertEqual(expanded["test3"], self.expected["test3"])
        self.assertEqual(expanded["test4"], self.expected["test4"])
        restore_includes(includes, self.doc, expanded, CommentedMap)
        # restoreInclude should make expanded look like self.doc
        self.assertEqual(expanded["test1"], self.doc["test1"])
        # XXX restoring lists not implemented:
        # self.assertEqual(expanded["test2"], self.doc["test2"])
        # self.assertEqual(expanded["test3"], self.doc["test3"])

    def test_diff(self):
        expectedOld = {"a": 1, "b": {"b1": 1, "b2": 1}, "d": 1, "f": {}}
        old = copy.copy(expectedOld)
        new = {"a": 1, "b": {"b1": 2, "b2": 1}, "c": 2, "e": None, "f": None}
        diff = diff_dicts(old, new)
        self.assertEqual(
            diff,
            {
                "b": {"b1": 2},
                "c": 2,
                "d": {"+%": "whiteout"},
                "e": None,
                "f": {"+%": "nullout"},
            },
        )
        newNew = merge_dicts(old, diff)
        self.assertEqual(newNew, new)
        self.assertEqual(old, expectedOld)
        patch_dict(old, new)
        self.assertEqual(old, new)

    def test_missingInclude(self):
        doc1 = CommentedMap(
            [("+/a/c", None), ("a", {"+/b": None}), ("b", {"c": {"d": 1}})]
        )
        includes, expanded = expand_doc(doc1, cls=CommentedMap)
        self.assertEqual(expanded, {"d": 1, "a": {"c": {"d": 1}}, "b": {"c": {"d": 1}}})
        self.assertEqual(len(includes), 2)

        doc1["missing"] = {"+/include-missing": None}
        with self.assertRaises(UnfurlError) as err:
            includes, expanded = expand_doc(doc1, cls=CommentedMap)

        doc1["missing"] = {"+include": "ensemble-template.yaml"}
        with self.assertRaises(UnfurlError) as err:
            includes, expanded = expand_doc(doc1, cls=CommentedMap)

        doc1["missing"] = {"?+include": "ensemble-template.yaml"}
        includes, expanded = expand_doc(doc1, cls=CommentedMap)

        doc2 = CommentedMap(
            [("+/a/d", None), ("a", {"+/e": None}), ("b", {"c": {"d": 1}})]
        )
        with self.assertRaises(UnfurlError) as err:
            includes, expanded = expand_doc(doc2, cls=CommentedMap)

        doc3 = CommentedMap(
            [("+?/a/d", None), ("a", {"+?/e": None}), ("b", {"c": {"d": 1}})]
        )
        includes, expanded = expand_doc(doc3, cls=CommentedMap)
        # missing includes are removed
        assert expanded == {"b": {"c": {"d": 1}}}, expanded


    def test_recursion(self):
        doc = {"test3": {"a": {"recurse": {"+/test3": None}}}}
        with self.assertRaises(UnfurlError) as err:
            includes, expanded = expand_doc(doc)
        self.assertEqual(
            str(err.exception),
            """recursive include "('test3',)" in "('test3', 'a', 'recurse')" when including +/test3""",
        )

        doc2 = {"test4": {"+../test4": None, "child": {}}}
        with self.assertRaises(UnfurlError) as err:
            includes, expanded = expand_doc(doc2)
        self.assertEqual(
            str(err.exception),
            """recursive include "['test4']" in "('test4',)" when including +../test4""",
        )

    def test_listmerge(self):
        a = {
            "spec": [{"a": "a"}],
        }
        b = {
            "spec": [{"+%": "merge", "b": "b"}]
        }

        expected = {
            "spec": [{"a": "a", "b": "b"}]
        }
        # second argument overrides first
        expanded = merge_dicts(a, b)
        assert expanded == expected

    def test_listmerge_dicts(self):
        a = {
            "spec": [{"a": {1: 1}}, "foo", {"c": "base"}],
        }
        b = {
            "spec": [{"b": "b"}, {"c": "override"}, {"a": {2:2}}]
        }

        expected = {
            "spec": [{"a": {1: 1, 2: 2}}, "foo", {"c": "override"}, {"b": "b"}]
        }
        # second argument overrides first
        expanded = merge_dicts(a, b)
        assert expanded == expected

class JobTest(unittest.TestCase):
    def test_lookupClass(self):
        assert lookup_class("Simple") is SimpleConfigurator

    # XXX rewrite
    # def test_runner(self):
    #   rootResource = NodeInstance('root')
    #   configurationSpec = ConfigurationSpec('test', 'instantiate', 'TestSubtaskConfigurator', 0)
    #   specs = [configurationSpec]
    #   runner = Runner(Manifest(rootResource, specs))
    #   job = runner.run(JobOptions(add=True))
    #   assert not job.unexpectedAbort, job.unexpectedAbort.stackInfo


class PickleTest(unittest.TestCase):
    def test_pickle(self):
        path = __file__ + "/../examples/mock-helm-ensemble.yaml"
        manifest = YamlManifest(path=path)
        pickled = pickle.dumps(manifest, -1)
        manifest2 = pickle.loads(pickled)
        # self.assertEqual(manifest.rootResource, manifest2.rootResource)


class OperationalInstanceTest(unittest.TestCase):
    def test_aggregate(self):
        ignoredError = OperationalInstance("error", "ignore")
        assert ignoredError.status == Status.error
        assert ignoredError.priority == Priority.ignore
        assert not ignoredError.operational
        assert not ignoredError.required

        requiredError = OperationalInstance("error", "required")
        assert requiredError.status == Status.error
        assert requiredError.priority == Priority.required
        assert requiredError.required
        assert not requiredError.operational

        aggregateOK = OperationalInstance(Status.ok)
        aggregateOK.dependencies = [ignoredError]
        assert aggregateOK.status == Status.ok

        aggregateError = OperationalInstance(Status.ok)
        aggregateError.dependencies = [ignoredError, requiredError]
        self.assertEqual(aggregateError.status, Status.error)

        aggregateError = OperationalInstance(Status.unknown)
        aggregateError.dependencies = [ignoredError, requiredError]
        self.assertEqual(aggregateError.status, Status.unknown)

        aggregateError = OperationalInstance(Status.ok)
        aggregateError.dependencies = [OperationalInstance("unknown", "optional")]
        self.assertEqual(aggregateError.status, Status.degraded)

        aggregateError = OperationalInstance(Status.unknown)
        aggregateError.dependencies = []
        self.assertEqual(aggregateError.status, Status.unknown)

        aggregateError = OperationalInstance(Status.ok)
        aggregateError.dependencies = []
        self.assertEqual(aggregateError.status, Status.ok)

        aggregate = OperationalInstance(Status.ok)
        aggregate.dependencies = [OperationalInstance("pending", "required")]
        self.assertEqual(aggregate.status, Status.pending)

        aggregateError = OperationalInstance(Status.ok)
        aggregateError.dependencies = [OperationalInstance("absent", "required")]
        self.assertEqual(aggregateError.status, Status.error)

        aggregate = OperationalInstance(Status.absent)
        aggregate.dependencies = [OperationalInstance("absent", "required")]
        self.assertEqual(aggregate.status, Status.absent)

    def test_nodestate(self):
        # considered active if status is error and not node state set
        assert OperationalInstance(Status.error).active
        # unknown status is treated as active too
        assert OperationalInstance(Status.unknown).active
        # but NodeState.error is treated as inactive
        assert not OperationalInstance(Status.error, state=NodeState.error).active
        assert OperationalInstance(state=NodeState.stopping).has_state(NodeState.created)
        assert not OperationalInstance(state=NodeState.stopping).has_state(NodeState.starting)
        assert not OperationalInstance(state=NodeState.deleting).has_state(NodeState.created)
        assert not OperationalInstance(state=NodeState.error).has_state(NodeState.created)

class RunTest(unittest.TestCase):
    """
    XXX:
    test status: report last state, report config changes, report plan
    test discover
    test failing configurations with abort, continue, revert (both capable of reverting and not)
    test incomplete runs
    test unexpected errors, terminations
    test locking
    test required metadata on resources
    """

    def test_manifest(self):
        simple = (
            """
    apiVersion: %s
    kind: Ensemble
    spec:
      instances:
        anInstance:
          template:
            type: unfurl.nodes.Default
            interfaces:
              Standard:
                operations:
                 configure:
                  implementation:    TestSubtask
                  inputs: {}
          readyState: pending
    """
            % API_VERSION
        )
        manifest = YamlManifest(simple)
        runner = Runner(manifest)
        output = io.StringIO()
        job = runner.run(JobOptions(add=True, out=output, startTime="test"))
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        # print(output.getvalue())
        # workDone includes subtasks
        assert len(job.workDone) == 2, job.workDone

        # manifest shouldn't have changed
        manifest2 = YamlManifest(output.getvalue())
        lock = manifest2.manifest.config["lock"]
        # this in-memory manifest doesn't use git repos so no repositories are included in the lock
        assert "runtime" in lock and len(lock["repositories"]) == 0
        self.assertEqual(
            manifest2.lastJob["summary"],
            "2 tasks (2 changed, 2 ok, 0 failed, 0 blocked, 0 unknown, 0 skipped)",
        )
        output2 = io.StringIO()
        job2 = Runner(manifest2).run(JobOptions(add=True, out=output2))
        assert not job2.unexpectedAbort, job2.unexpectedAbort.get_stack_trace()

        # should not find any tasks to run
        assert len(job2.workDone) == 0, job2.workDone
        self.maxDiff = None
        self.assertEqual(output.getvalue(), output2.getvalue())

    def test_template_inheritance(self):
        manifest = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
configurators:
  step1:
    spec:
      className: "TestSubtaskConfigurator"
      majorVersion: 0
templates:
  base:
    configurations:
      step1:
        +/configurators/step1:
root:
  instances:
    cloud3: #key is resource name
      +/templates/base:
      +/templates/production:
"""
        with self.assertRaises(UnfurlError) as err:
            YamlManifest(manifest)
        self.assertIn("missing includes: ['+/templates/production:']", str(err.exception))

    def test_sensitive_logging(self):
        handler = logging.getLogger().handlers[0]
        assert isinstance(handler, logs.ColorHandler)
        old_stream = handler.stream
        try:
            handler.stream = io.StringIO()

            logger = logs.getLogger("unfurl")
            logger.warning("test1 %s", sensitive_str("sensitive"))

            logger = logging.getLogger("another")
            logger.critical("test2 %s", sensitive_str("sensitive"))

            log_output = handler.stream.getvalue() 
            # rich color handler does weird escaping
            # self.assertIn(sensitive_str.redacted_str, log_output)
            self.assertIn("<REDACTED", log_output)
            self.assertNotIn("sensitive", log_output)
        finally:
            handler.stream = old_stream


class TestInterface:
    def __init__(self, interfaceName, resource):
        self.name = interfaceName
        self.resource = resource


class InterfaceTest(unittest.TestCase):
    """
    .interfaces:
      inherit: ClassName
      default:
      filter:
    """

    def test_interface(self):
        r = NodeInstance("test")
        r.add_interface(TestInterface)
        className = __name__ + ".TestInterface"
        self.assertEqual(r.attributes[".interfaces"], {className: className})
        i = r.get_interface(className)
        assert i, "interface not found"
        self.assertIs(r, i.resource)
        self.assertEqual(i.name, className)
        self.assertIs(r._interfaces[className], i)

    # XXX test ::container[.interfaces=Container]


class FileTestConfigurator(Configurator):
    def run(self, task):
        assert self.can_run(task)
        assert task.target.attributes["file"] == "foo.txt", task.target.attributes
        assert os.path.isabs(task.inputs["path"]), task.inputs
        assert task.inputs["contents"] == "test", task.inputs["contents"]

        filevalue = task.query({"ref": {"file": "foo.txt"}}, wantList='result')
        assert filevalue[0].external.type == "file"

        filevalue = task.query(
            {"ref": {"file": "foo.txt"}, "foreach": "path"}, wantList='result'
        )
        assert filevalue[0].external.type == "file"

        value = task.query({"ref": {"file": "foo.txt"}})
        yield task.done(True, Status.ok, result=value)


class FileTest(unittest.TestCase):
    def test_fileRef(self):
        simple = (
            """
    apiVersion: %s
    kind: Ensemble
    spec:
      service_template:
        topology_template:
          node_templates:
            test:
              type: tosca.nodes.Root
              properties:
                file:
                  eval:
                    file: foo.txt
              interfaces:
                Standard:
                    create:
                      implementation:  FileTest
                      inputs:
                        path:
                          eval: file::path
                        contents:
                          eval: file::contents
    """
            % API_VERSION
        )
        cliRunner = CliRunner()
        with cliRunner.isolated_filesystem():  # as tmpDir
            manifest = YamlManifest(simple, path=".")
            runner = Runner(manifest)
            output = io.StringIO()
            with open("foo.txt", "w") as f:
                f.write("test")
            job = runner.run(JobOptions(add=True, out=output, startTime="test"))
        task = list(job.workDone.values())[0]
        self.assertEqual(task.result.result, "foo.txt")

@pytest.mark.parametrize(
    "readonly",
    [False, True],
)
def test_template_includes(readonly):
        template = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
dsl:
  bar:
    +&: bar
    c: 4
spec:
  a: 1
  b: 2
status: {}
    """
        cliRunner = CliRunner()
        with cliRunner.isolated_filesystem():  # as tmpDir
            with open("template.yaml", "w") as f:
                f.write(template)

            instanceYaml = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
dsl:
  foo:
    +&: foo
    d: 5
  +./bar:
  +./foo:
+include:
  file: template.yaml
+include2:
  file: template.yaml
+?include: missing.yaml
+?include2: missing.yaml
spec:
  +*foo:
  +*bar:
  b: 3
      """

            with open("ensemble.yaml", "w") as f:
                f.write(instanceYaml)

            manifest = YamlManifest(localEnv=LocalEnv(readonly=readonly))
            assert manifest.manifest.expanded["dsl"]["c"] == 4
            assert manifest.manifest.expanded["dsl"]["d"] == 5
            assert manifest.manifest.expanded["spec"]["a"] == 1
            assert manifest.manifest.expanded["spec"]["b"] == 3
            assert manifest.manifest.expanded["spec"]["c"] == 4
            assert manifest.manifest.expanded["spec"]["d"] == 5
            # XXX these shouldn't be in expanded:
            # assert "+include" not in manifest.manifest.expanded
            # assert "+?include" not in manifest.manifest.expanded
            # assert manifest.manifest.config["dsl"]["foo"].anchor.value == "foo"
            # assert manifest.manifest.config["dsl"]["foo"].anchor.always_dump

            if not readonly:
                output = io.StringIO()
                manifest.dump(output)
                config = YamlConfig(output.getvalue())
                assert config.config["+include"] == {"file": "template.yaml"}
                assert config.config["+include2"] == {"file": "template.yaml"}
                assert config.config["+?include"] == "missing.yaml"
                assert config.config["+?include2"] == "missing.yaml"
                assert "a" not in config.config["spec"]

            configYaml = """
kind: Project
+?include: local/unfurl.yaml
a:
  b: 1
"""
            config = YamlConfig(configYaml, readonly=readonly)
            assert config.expanded.base_dir == "."
            assert config.expanded["a"].base_dir == "."

    # XXX
    # def test_change(self):
    #     """
    # config parameter: file.path
    # run...
    # touch file...
    # run again...
    # assert it triggers update
    # """
    #


class ImportTestConfigurator(Configurator):
    def run(self, task):
        assert self.can_run(task)
        assert task.target.attributes["test"]
        assert task.target.attributes["mapped1"]
        yield task.done(True, Status.ok)


class ImportTest(unittest.TestCase):
    def test_import(self):
        foreign = (
            """
    apiVersion: %s
    kind: Ensemble
    spec:
     instances:
      foreign:
        attributes:
          prop1: ok
          prop2: not-a-number
    """
            % API_VERSION
        )
        runner = CliRunner()
        with runner.isolated_filesystem():
            with open("foreignmanifest.yaml", "w") as f:
                f.write(foreign)

            importer = (
                """
apiVersion: %s
kind: Ensemble
metadata:
  uri: https://myrepos.com/foo.git#:ensemble/ensemble.yaml
environment:
 external:
  test:
    manifest:
      file: foreignmanifest.yaml
    instance: foreign # default is root
    # attributes: # queries into resource
    schema: # expected schema for attributes
      prop2:
       type: number
      prop3:
        type: string
        default: 'default'
spec:
  instances:
    importer:
      template: importer
  service_template:
    topology_template:
      node_templates:
        importer:
          type: tosca.nodes.Root
          properties:
            test:
              eval:
                external: test
            mapped1:
              eval:
                external: test
              select: prop1
            mapped2:
              eval:
                external: test
              select: prop2
            mapped3:
              eval:
                external: test
              select: prop3
          interfaces:
            Standard:
                create: ImportTest
      """
                % API_VERSION
            )
            with open("ensemble.yaml", "w") as f:
                f.write(importer)
            manifest = YamlManifest(path="ensemble.yaml")
            root = manifest.get_root_resource()
            importerResource = root.find_resource("importer")
            assert manifest.uri == "https://myrepos.com/foo.git#:ensemble/ensemble.yaml", manifest.uri
            assert root.uri == manifest.uri + "?::root"
            assert importerResource.uri == manifest.uri + "?::importer"
            # assert importing.attributes['test']
            assert root.imports["test"]
            self.assertEqual(importerResource.attributes["mapped1"], "ok")
            with self.assertRaises(UnfurlValidationError) as err:
                importerResource.attributes["mapped2"]
            self.assertIn("schema validation failed", str(err.exception))
            self.assertEqual(importerResource.attributes["mapped3"], "default")
            job = Runner(manifest).run(JobOptions(add=True, startTime="time-to-test"))
            assert job.status == Status.ok, job.summary()
            manifest.lock()
            with self.assertRaises(UnfurlError) as err:
                manifest.lock()  # can't lock twice
            self.assertIn("already locked", str(err.exception))

@pytest.mark.parametrize("include_paths, exclude_paths, target_path, expected", [
    (["/path/to/include"], [], "/path/to/include/subdir", True),
    (["/path/to"], ["/path/to/exclude"], "/path/to/exclude/subdir", False),
    ([], [os.path.abspath(".")], "./subdir", False),  # compare by absolute paths
    (["/path/to"], ["/path/to"], "/path/to/exclude/subdir", False), # exclude takes precedence
    ([], [], "/path/to/target", True),
    (["/path/to/include"], ["/"], "/path/to/target", False),
    (["/path/to/target/"], ["/"], "/path/to/target", True),
    (["/path/to/include"], [], "/path/to/include/subdir/subsubdir", True),
    ([], ["/path/to/exclude"], "/path/to/exclude/subdir/subsubdir", False),
])
def test_should_include_path(include_paths, exclude_paths, target_path, expected):
    assert should_include_path(include_paths, exclude_paths, target_path) == expected
