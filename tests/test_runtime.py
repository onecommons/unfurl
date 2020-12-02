import unittest
from unfurl.runtime import Status, Priority, NodeInstance, OperationalInstance
from unfurl.job import JobOptions, Runner
from unfurl.configurator import Configurator, ConfigurationSpec
from unfurl.yamlmanifest import YamlManifest
from unfurl.yamlloader import YamlConfig
from unfurl.util import (
    UnfurlError,
    UnfurlValidationError,
    lookupClass,
    API_VERSION,
    sensitive_str,
)
from unfurl.merge import (
    expandDoc,
    restoreIncludes,
    diffDicts,
    mergeDicts,
    patchDict,
    parseMergeKey,
)
from ruamel.yaml.comments import CommentedMap
import traceback
import six
import datetime
import copy
import os.path
import pickle
from click.testing import CliRunner
import logging
from unfurl import initLogging


class SimpleConfigurator(Configurator):
    def run(self, task):
        assert self.canRun(task)
        yield task.done(True, Status.ok)


simpleConfigSpec = ConfigurationSpec("subtask", "instantiate", "Simple", 0)


class TestSubtaskConfigurator(Configurator):
    def run(self, task):
        assert self.canRun(task)
        configuration = yield task.createSubTask(simpleConfigSpec)
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
    }

    expected = {
        "test1": {"a": {"b": 2}, "c": "c", "d": "val", "e": "e"},
        "test2": [1, "a", "b", "+t4", "a", "b"],
        "test3": {"list": [1, 2, 3]},
    }

    def test_expandDoc(self):
        includes, expanded = expandDoc(self.doc, cls=CommentedMap)
        self.assertEqual(
            includes,
            {
                ("test1",): [(parseMergeKey("+/t2"), None)],
                ("test1", "a"): [(parseMergeKey("+/t1"), None)],
                ("test1", "d"): [(parseMergeKey("+/t3"), None)],
                ("test2", 1): [(parseMergeKey("+/t4"), "")],
                ("test2", 3): [(parseMergeKey("+/t4"), None)],
                ("test3",): [(parseMergeKey("+/base"), None)],
            },
        )
        self.assertEqual(expanded["test1"], self.expected["test1"])
        self.assertEqual(expanded["test2"], self.expected["test2"])
        self.assertEqual(expanded["test3"], self.expected["test3"])
        restoreIncludes(includes, self.doc, expanded, CommentedMap)
        # restoreInclude should make expanded look like self.doc
        self.assertEqual(expanded["test1"], self.doc["test1"])
        # XXX restoring lists not implemented:
        # self.assertEqual(expanded["test2"], self.doc["test2"])
        # self.assertEqual(expanded["test3"], self.doc["test3"])

    def test_diff(self):
        expectedOld = {"a": 1, "b": {"b1": 1, "b2": 1}, "d": 1}
        old = copy.copy(expectedOld)
        new = {"a": 1, "b": {"b1": 2, "b2": 1}, "c": 2}
        diff = diffDicts(old, new)
        self.assertEqual(diff, {"b": {"b1": 2}, "c": 2, "d": {"+%": "delete"}})
        newNew = mergeDicts(old, diff)
        self.assertEqual(newNew, new)
        self.assertEqual(old, expectedOld)
        patchDict(old, new)
        self.assertEqual(old, new)

    def test_missingInclude(self):
        doc1 = CommentedMap(
            [("+/a/c", None), ("a", {"+/b": None}), ("b", {"c": {"d": 1}})]
        )
        includes, expanded = expandDoc(doc1, cls=CommentedMap)
        self.assertEqual(expanded, {"d": 1, "a": {"c": {"d": 1}}, "b": {"c": {"d": 1}}})
        self.assertEqual(len(includes), 2)

        doc1["missing"] = {"+/include-missing": None}
        with self.assertRaises(UnfurlError) as err:
            includes, expanded = expandDoc(doc1, cls=CommentedMap)

    def test_recursion(self):
        doc = {"test3": {"a": {"recurse": {"+/test3": None}}}}
        with self.assertRaises(UnfurlError) as err:
            includes, expanded = expandDoc(doc)
        self.assertEqual(
            str(err.exception),
            """recursive include "('test3',)" in "('test3', 'a', 'recurse')" when including +/test3""",
        )

        doc2 = {"test4": {"+../test4": None, "child": {}}}
        with self.assertRaises(UnfurlError) as err:
            includes, expanded = expandDoc(doc2)
        self.assertEqual(
            str(err.exception),
            """recursive include "['test4']" in "('test4',)" when including +../test4""",
        )


class JobTest(unittest.TestCase):
    def test_lookupClass(self):
        assert lookupClass("Simple") is SimpleConfigurator

    # XXX rewrite
    # def test_runner(self):
    #   rootResource = NodeInstance('root')
    #   configurationSpec = ConfigurationSpec('test', 'instantiate', 'TestSubtaskConfigurator', 0)
    #   specs = [configurationSpec]
    #   runner = Runner(Manifest(rootResource, specs))
    #   job = runner.run(JobOptions(add=True))
    #   assert not job.unexpectedAbort, job.unexpectedAbort.stackInfo


class PickleTest(unittest.TestCase):
    @unittest.skipIf(six.PY2, "XXX fix pickling in 2.7")
    def test_pickle(self):
        path = __file__ + "/../examples/helm-manifest.yaml"
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
    kind: Manifest
    spec:
      instances:
        anInstance:
          # template: foo
          interfaces:
            Standard:
              operations:
               configure:
                implementation:    TestSubtask
                inputs: {}
    """
            % API_VERSION
        )
        manifest = YamlManifest(simple)
        runner = Runner(manifest)
        self.assertEqual(runner.taskCount, 0)
        output = six.StringIO()
        job = runner.run(JobOptions(add=True, out=output, startTime="test"))
        assert not job.unexpectedAbort, job.unexpectedAbort.getStackTrace()
        # workDone includes subtasks
        assert len(job.workDone) == 2, job.workDone

        # manifest shouldn't have changed
        manifest2 = YamlManifest(output.getvalue())
        lock = manifest2.manifest.config["lock"]
        assert "runtime" in lock and len(lock["repositories"]) == 3
        self.assertEqual(
            manifest2.lastJob["summary"],
            "2 tasks (2 changed, 2 ok, 0 failed, 0 unknown, 0 skipped)",
        )
        output2 = six.StringIO()
        job2 = Runner(manifest2).run(JobOptions(add=True, out=output2))
        assert not job2.unexpectedAbort, job2.unexpectedAbort.getStackTrace()

        # should not find any tasks to run
        assert len(job2.workDone) == 0, job2.workDone
        self.maxDiff = None
        self.assertEqual(output.getvalue(), output2.getvalue())

    def test_template_inheritance(self):
        manifest = """
apiVersion: unfurl/v1alpha1
kind: Manifest
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
        self.assertIn("missing includes: [+/templates/production]", str(err.exception))

    def test_sensitive_logging(self):
        try:
            _logHandler, oldLevel = initLogging(logging.DEBUG)
            oldStream = _logHandler.stream
            _logHandler.stream = six.StringIO()

            logger = logging.getLogger("unfurl")
            logger.info("test1 %s", sensitive_str("sensitive"))

            logger = logging.getLogger("another")
            logger.debug("test2 %s", sensitive_str("sensitive"))

            logOutput = _logHandler.stream.getvalue()
            self.assertIn(sensitive_str.redacted_str, logOutput)
            self.assertNotIn("sensitive", logOutput)
        finally:
            _logHandler.stream = oldStream
            initLogging(oldLevel)


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
        r.addInterface(TestInterface)
        className = __name__ + ".TestInterface"
        self.assertEqual(r.attributes[".interfaces"], {className: className})
        i = r.getInterface(className)
        assert i, "interface not found"
        self.assertIs(r, i.resource)
        self.assertEqual(i.name, className)
        self.assertIs(r._interfaces[className], i)

    # XXX test ::container[.interfaces=Container]


class FileTestConfigurator(Configurator):
    def run(self, task):
        assert self.canRun(task)
        assert task.target.attributes["file"] == "foo.txt", task.target.attributes
        assert os.path.isabs(task.inputs["path"]), task.inputs
        assert task.inputs["contents"] == "test", task.inputs["contents"]

        filevalue = task.query({"ref": {"file": "foo.txt"}}, wantList=True)
        assert filevalue._attributes[0].external.type == "file"

        filevalue = task.query(
            {"ref": {"file": "foo.txt"}, "foreach": "path"}, wantList=True
        )
        assert filevalue._attributes[0].external.type == "file"

        value = task.query({"ref": {"file": "foo.txt"}})
        yield task.done(True, Status.ok, result=value)


class FileTest(unittest.TestCase):
    def test_fileRef(self):
        simple = (
            """
    apiVersion: %s
    kind: Manifest
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
                          ref: file::path
                        contents:
                          ref: file::contents
    """
            % API_VERSION
        )
        manifest = YamlManifest(simple)
        runner = Runner(manifest)
        output = six.StringIO()
        cliRunner = CliRunner()
        with cliRunner.isolated_filesystem():  # as tmpDir
            with open("foo.txt", "w") as f:
                f.write("test")
            job = runner.run(JobOptions(add=True, out=output, startTime="test"))
        task = list(job.workDone.values())[0]
        self.assertEqual(task.result.result, "foo.txt")

    def test_template_includes(self):
        template = """
apiVersion: unfurl/v1alpha1
kind: Manifest
dsl:
  bar: &bar
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
kind: Manifest
dsl:
  foo: &foo
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

            manifest = YamlManifest(instanceYaml)
            assert manifest.manifest.expanded["dsl"]["c"] == 4
            assert manifest.manifest.expanded["dsl"]["d"] == 5
            assert manifest.manifest.expanded["spec"]["a"] == 1
            assert manifest.manifest.expanded["spec"]["b"] == 3
            assert manifest.manifest.expanded["spec"]["c"] == 4
            assert manifest.manifest.expanded["spec"]["d"] == 5
            # XXX these shouldn't be in expanded:
            # assert "+include" not in manifest.manifest.expanded
            # assert "+?include" not in manifest.manifest.expanded
            assert manifest.manifest.config["dsl"]["foo"].anchor.value == "foo"
            assert manifest.manifest.config["dsl"]["foo"].anchor.always_dump

            output = six.StringIO()
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
            config = YamlConfig(configYaml)
            assert config.expanded.baseDir == "."
            assert config.expanded["a"].baseDir == "."

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
        assert self.canRun(task)
        assert task.target.attributes["test"]
        assert task.target.attributes["mapped1"]
        yield task.done(True, Status.ok)


class ImportTest(unittest.TestCase):
    def test_import(self):
        foreign = (
            """
    apiVersion: %s
    kind: Manifest
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
kind: Manifest
context:
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
            manifest = YamlManifest(importer)
            root = manifest.getRootResource()
            importerResource = root.findResource("importer")
            # assert importing.attributes['test']
            assert root.imports["test"]
            self.assertEqual(importerResource.attributes["mapped1"], "ok")
            with self.assertRaises(UnfurlValidationError) as err:
                importerResource.attributes["mapped2"]
            self.assertIn("schema validation failed", str(err.exception))
            self.assertEqual(importerResource.attributes["mapped3"], "default")
            job = Runner(manifest).run(JobOptions(add=True, startTime="time-to-test"))
            assert job.status == Status.ok, job.summary()
