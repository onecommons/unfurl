import unittest
import click
from click.testing import CliRunner
from giterop.__main__ import cli
from giterop import __version__

class CliTest(unittest.TestCase):

  def test_help(self):
    runner = CliRunner()
    result = runner.invoke(cli, [])
    assert result.output.startswith("Usage: cli [OPTIONS] COMMAND [ARGS]"), result.output
    self.assertEqual(result.exit_code, 0)

  def test_version(self):
    runner = CliRunner()
    result = runner.invoke(cli, ['version'])
    self.assertEqual(result.exit_code, 0)
    self.assertEqual(result.output.strip(), "giterop version %s" % __version__)

  def test_run(self):
    runner = CliRunner()
    with runner.isolated_filesystem():
      with open('manifest.yaml', 'w') as f:
        f.write('invalid manifest')
      result = runner.invoke(cli, ['run'])
      self.assertEqual(result.exit_code, 1)
      self.assertEqual(result.output.strip(), "malformed YAML or JSON document\nError: malformed YAML or JSON document")
