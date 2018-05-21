import giterop.util
import unittest
import os
import os.path
import warnings
from giterop import *
from giterop.ansibleconfigurator import *

"""
ansible_connection=ssh
ansible_ssh_user=ubuntu
ansible_ssh_private_key_file=~/.ssh/fairblocker.pem

testansible:
  #sshconfig: defaultconfig
  inventory:
    all:
      hosts:
        mail.example.com:
      children:
        webservers:
          hosts:
            foo.example.com:
            bar.example.com:
        dbservers:
          hosts:
            one.example.com:
            two.example.com:
            three.example.com:
             ansible_port: 5555
             ansible_host: 192.0.2.50
          vars:
            ntp_server: ntp.atlanta.example.com
            proxy: proxy.atlanta.example.com
all:
  children
    ref: ".root[template:awsAccount]"
    foreach:
      key:   ref: ".:name"
             vars:
      value:
        hosts:
          ref: ".descendents"
          foreach:
            key: ref: hostname
            value:
              template: ansible_hostvars
    - webserver: ref: ".descendents[shape:webserver]"
    - name:
      members:
      vars:
  hostvars:
   foo: ref: .foo
  provides:
   - target: ref: '$host:.descendents[hostname=$host]:hostname'
             vars:
              hostname: ref: '$host:hostname'
     metadata:
      foo: ref: '$host:$key'
      bar: ref: '$host:$key'


template:
 ansible_template:
  ansible_port?: ref: port
  ansible_host: ref: host
"""

class AnsibleTest(unittest.TestCase):
  def setUp(self):
    # need to call this again on python 2.7:
    giterop.util.initializeAnsible()
    try:
      # Ansible generates tons of ResourceWarnings
      warnings.simplefilter("ignore", ResourceWarning)
    except:
      # python 2.x doesn't have ResourceWarning
      pass
    self.results = {}

  def runPlaybook(self, args=None):
    return runPlaybooks([os.path.join(os.path.dirname(__file__),'testplaybook.yaml')], 'localhost,', {
    'ansible_connection': 'local',
    'extra': 1
    }, args)

  def test_runplaybook(self):
    results = self.runPlaybook()
    self.results['runplaybook'] = results
    self.assertEqual('test',
      results.variableManager._nonpersistent_fact_cache['localhost'].get('one_fact'))

    hostfacts = results.variableManager._nonpersistent_fact_cache['localhost']
    self.assertEqual(hostfacts['one_fact'], 'test')
    self.assertEqual(hostfacts['echoresults']['stdout'], "hello")

  def test_verbosity(self):
    results = self.runPlaybook()
    # task test-verbosity was skipped
    assert not results.resultsByStatus.ok.get('test-verbosity')
    assert results.resultsByStatus.skipped.get('test-verbosity')

    results = self.runPlaybook(['-vv'])
    # task test-verbosity was ok this time
    assert results.resultsByStatus.ok.get('test-verbosity')
    assert not results.resultsByStatus.skipped.get('test-verbosity')
