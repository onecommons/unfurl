import unittest
import os
import traceback
from collections import MutableSequence
from click.testing import CliRunner
from unfurl.__main__ import cli
from unfurl import __version__
from unfurl.configurator import Configurator

manifest = """
apiVersion: unfurl/v1alpha1
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
        # uses jinja2 native types to evaluate to a list
        aListOfItems:
            eval:
              template: >
                [{% for key, value in aLocalDict.items() %}
                  {
                    'key': '{{ key }}',
                    'value': '{{ value | b64encode }}'
                  },
                {% endfor %}]
            vars:
              aLocalDict:
               eval:
                # XXX test with secret instead (need to propagate secretness)
                secret: aDict
            # trace: 1
      interfaces:
        Standard:
          create: test_cli.CliTestConfigurator
"""

localConfig = """
defaults: #used if manifest isnt found in `manifests` list below
 secret:
  attributes:
    aDict:
      key1: a string
      key2: 2
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
        aDict:
          key1: a string
          key2: 2
"""

class CliTestConfigurator(Configurator):
  def run(self, task):
    attrs = task.target.attributes
    assert isinstance(attrs['aListOfItems'], MutableSequence), type(attrs['aListOfItems'])
    # sort for python2
    assert sorted(attrs['aListOfItems'], key=lambda k: k['key']) == [{
        'key': 'key1',
        'value': 'YSBzdHJpbmc='
      },
      {
        'key': 'key2',
        'value': 'Mg=='
      }], attrs['aListOfItems']
    assert attrs['local1'] == 'found', attrs['local1']
    assert attrs['local2'] == 1, attrs['local2']
    assert attrs['testApikey'] == 'secret', 'failed to get secret environment variable, maybe DelegateAttributes is broken?'
    assert attrs._attributes['aListOfItems'].external.type == 'sensitive'
    # XXX this should be marked as secret so it should serializes as "[REDACTED]"
    # attrs['copyofAListOfItems'] = attrs['aListOfItems']
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
    self.assertEqual(result.output.strip(), "unfurl version %s" % __version__)

  def test_badargs(self):
    runner = CliRunner()
    result = runner.invoke(cli, ['--badarg'])
    self.assertEqual(result.exit_code, 2, result)

  def test_run(self):
    runner = CliRunner()
    with runner.isolated_filesystem():
      with open('manifest.yaml', 'w') as f:
        f.write('invalid manifest')

      result = runner.invoke(cli, ['run'])
      self.assertEqual(result.exit_code, 1, result)
      # XXX log handler is writing to the CliRunner's output stream
      self.assertEqual(result.output.strip(), 'Unable to create job')

  def test_localConfig(self):
    # test loading the default manifest declared in the local config
    # test locals and secrets:
    #    declared attributes and default lookup
    #    inherited from default (inheritFrom)
    #    verify secret contents isn't saved in config
    os.environ['GEO_TESTAPIKEY'] = 'secret'
    runner = CliRunner()
    with runner.isolated_filesystem() as tempDir:
      with open('unfurl.yaml', 'w') as local:
        local.write(localConfig)
      repoDir = 'git'
      os.mkdir(repoDir)
      os.chdir(repoDir)
      with open('default-manifest.yaml', 'w') as f:
        f.write(manifest)
      result = runner.invoke(cli, ['-vvv', 'deploy', '--jobexitcode', 'degraded'])
      # uncomment this to see output:
      print("result.output", result.exit_code, result.output)
      assert not result.exception, '\n'.join(traceback.format_exception(*result.exc_info))
      self.assertEqual(result.exit_code, 0, result.output)
