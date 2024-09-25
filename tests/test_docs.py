import unittest
import os
import glob
from unfurl.localenv import LocalConfig
from unfurl.yamlmanifest import YamlManifest, _basepath
from unfurl.yamlloader import YamlConfig

basedir = os.path.join(os.path.dirname(__file__), "..", "docs", "examples")


class DocsTest(unittest.TestCase):
    def test_schemas(self):
        assert LocalConfig(os.path.join(basedir, "unfurl.yaml"))
        assert YamlManifest(path=os.path.join(basedir, "ensemble.yaml"))
        assert YamlConfig(
            path=os.path.join(basedir, "job.yaml"),
            schema=os.path.join(_basepath, "changelog-schema.json"),
        )
        # path = os.path.join(basedir, "service-template.yaml")
        # serviceTemplate = YamlConfig(path=path)
        # assert ToscaSpec(serviceTemplate.config, path=path)

    def test_python_snippets(self):
        python_files = glob.glob(os.path.join(basedir, "*.py"))

        required_imports = """
import unfurl
import tosca
from tosca import Attribute, Eval, Property, operation, GB, MB
"""

        for py_file in python_files:
            with self.subTest(py_file=py_file):
                with open(py_file, "r") as f:
                    code = f.read()

                # Add the required imports at the beginning of the code
                full_code = required_imports + "\n" + code

                # Execute the code to ensure it's valid
                print(f"Executing {py_file}")
                exec(full_code, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
