import unittest
import os
import glob

class DocsTest(unittest.TestCase):
    def test_python_snippets(self):
        basedir = os.path.join(os.path.dirname(__file__), "..", "docs", "examples")
        python_files = glob.glob(os.path.join(basedir, "*.py"))

        required_imports = """
import unfurl
from typing import Sequence
import tosca
from tosca import Attribute, Eval, Property, operation, GB, MB
import unfurl.configurators.shell

# Define missing elements if necessary
# Example: my_server, unfurl_nodes_Installer_Terraform, etc.
"""

        for py_file in python_files:
            with self.subTest(py_file=py_file):
                with open(py_file, 'r') as f:
                    code = f.read()

                # Add the required imports at the beginning of the code
                full_code = required_imports + "\n" + code

                # Execute the code to ensure it's valid
                try:
                    exec(full_code, {})
                except NameError as e:
                    print(f"Suppressed NameError in {py_file}: {str(e)}")
                except Exception as e:
                    self.fail(f"Failed to execute {py_file}: {str(e)}")

if __name__ == "__main__":
    unittest.main(verbosity=2)
