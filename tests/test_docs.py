import unittest
import os
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
