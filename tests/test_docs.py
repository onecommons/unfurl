import unittest
import os
from .test_dsl import _to_yaml
from toscaparser.tosca_template import ToscaTemplate
from unfurl.localenv import LocalConfig
from unfurl.yamlmanifest import YamlManifest, _basepath
from unfurl.yamlloader import YamlConfig


class DocsTest(unittest.TestCase):
    def test_schemas(self):
        basedir = os.path.join(os.path.dirname(__file__), "..", "docs", "examples")
        assert LocalConfig(os.path.join(basedir, "unfurl.yaml"))
        assert YamlManifest(path=os.path.join(basedir, "ensemble.yaml"))
        assert YamlConfig(
            path=os.path.join(basedir, "job.yaml"),
            schema=os.path.join(_basepath, "changelog-schema.json"),
        )

    def test_python_example(self):
        basedir = os.path.join(os.path.dirname(__file__), "..", "docs", "examples")
        yaml_template = ToscaTemplate(
            path=os.path.join(basedir, "service-template.yaml")
        )
        with open(os.path.join(basedir, "service_template.py")) as pyfile:
            from_py = _to_yaml(pyfile.read(), True)
        assert from_py["topology_template"]["outputs"] == yaml_template.topology_template._tpl_outputs()
        assert from_py["topology_template"]["inputs"] == yaml_template.topology_template._tpl_inputs()
