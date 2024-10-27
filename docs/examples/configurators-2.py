@operation(name="configure", outputs={"fact1": None})
def test_remote_configure(self, **kw):
    return unfurl.configurators.ansible.AnsibleConfigurator(
        playbook="""
        - set_fact:
            fact1: "{{ '.name' | eval }}"
        - name: Hello
          command: echo "{{ fact1 }}"
        """
    )

test_remote = tosca.nodes.Root()
test_remote.set_operation(test_remote_configure)
