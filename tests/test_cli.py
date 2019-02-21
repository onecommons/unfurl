import unittest
import click
import os
import traceback
from click.testing import CliRunner
from giterop.__main__ import cli
from giterop import __version__
from giterop.util import GitErOpError, GitErOpValidationError, VERSION
from giterop.runtime import Configurator

manifest = """
apiVersion: %s
kind: Manifest
imports:
  local:
    properties:
      prop2:
       type: number
root:
  spec:
    attributes:
      local1:
        #eval:
        #  external: local
        #foreach: prop1
        eval:
          local: prop1
      local2:
        eval:
          external: local
        foreach: prop2
    configurations:
      test:
        className: %s.CliTestConfigurator
        majorVersion: 0
        priority: required
""" % (VERSION, __name__)

localConfig = """
defaults: #for undeclared manifests
 local:
 secret:

manifests:
  - path: git/default-manifest.yaml
    local:
      attributes:
        prop1: 'found'
        prop2: 1
    secret:
"""

class CliTestConfigurator(Configurator):
  def run(self, task):
    attrs = task.currentConfig.resource.attributes
    assert attrs['local1'] == 'found', attrs['local1']
    assert attrs['local2'] == 1, attrs['local2']
    yield task.createResult(True, False, "ok")

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

  def test_localConfig(self):
    # test loading the default manifest declared in the local config
    # test locals and secrets:
    #    declared attributes and default lookup
    #    inherited from default (inheritFrom)
    runner = CliRunner()
    with runner.isolated_filesystem():  # as tempDir:
      with open('giterop.yaml', 'w') as local:
        local.write(localConfig)
      repoDir = 'git'
      os.mkdir(repoDir)
      os.chdir(repoDir)
      with open('default-manifest.yaml', 'w') as f:
        f.write(manifest)
      result = runner.invoke(cli, ['run', '--jobexitcode', 'degraded'])
      assert not result.exception, '\n'.join(traceback.format_exception(*result.exc_info))
      self.assertEqual(result.exit_code, 0)
