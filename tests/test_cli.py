import unittest
import click
import os
import traceback
from click.testing import CliRunner
from giterop.__main__ import cli
from giterop import __version__
from giterop.util import GitErOpError, GitErOpValidationError, VERSION
from giterop.configurator import Configurator

manifest = """
apiVersion: %s
kind: Manifest
imports:
  local:
    properties:
      prop2:
       type: number
spec:
  node_templates:
    test:
      type: tosca.nodes.Root
      properties:
        local1:
          eval:
            local: prop1
        local2:
          eval:
            external: local
          foreach: prop2
        testApikey:
          eval:
           secret: testApikey
      interfaces:
        Standard:
          create: %s.CliTestConfigurator
""" % (VERSION, __name__)

localConfig = """
defaults: #used if manifest isnt found in `manifests` list below
 secret:
  attributes:
    default: # if key isn't found, apply this:
      q: # quote
        eval:
          lookup:
            env: "GEO_{{ key | upper }}"

instances:
  - file: git/default-manifest.yaml
    local:
      attributes:
        prop1: 'found'
        prop2: 1
"""

class CliTestConfigurator(Configurator):
  def run(self, task):
    attrs = task.target.attributes
    assert attrs['local1'] == 'found', attrs['local1']
    assert attrs['local2'] == 1, attrs['local2']
    assert attrs['testApikey'].reveal == 'secret', 'failed to reveal environment variable, maybe DelegateAttributes is broken?'
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
    self.assertEqual(result.exit_code, 0, result)
    self.assertEqual(result.output.strip(), "giterop version %s" % __version__)

  def test_run(self):
    runner = CliRunner()
    with runner.isolated_filesystem():
      with open('manifest.yaml', 'w') as f:
        f.write('invalid manifest')
      result = runner.invoke(cli, ['run'])
      self.assertEqual(result.exit_code, 1)
      self.assertIn("invalid YAML document", result.output.strip())

  def test_localConfig(self):
    # test loading the default manifest declared in the local config
    # test locals and secrets:
    #    declared attributes and default lookup
    #    inherited from default (inheritFrom)
    #    verify secret contents isn't saved in config
    os.environ['GEO_TESTAPIKEY'] = 'secret'
    runner = CliRunner()
    with runner.isolated_filesystem() as tempDir:
      with open('giterop.yaml', 'w') as local:
        local.write(localConfig)
      repoDir = 'git'
      os.mkdir(repoDir)
      os.chdir(repoDir)
      with open('default-manifest.yaml', 'w') as f:
        f.write(manifest)
      result = runner.invoke(cli, ['-vvv', 'deploy', '--jobexitcode', 'degraded'])
      self.assertEqual(result.exit_code, 0, result.output)
      assert not result.exception, '\n'.join(traceback.format_exception(*result.exc_info))
