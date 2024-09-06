@operation(name="configure", outputs={"fact1": None})
def test_remote_configure(**kw):
    return unfurl.configurators.ansible.AnsibleConfigurator(
        playbook=Eval(
            {
                "q": [
                    {"set_fact": {"fact1": "{{ '.name' | eval }}"}},
                    {"name": "Hello", "command": 'echo "{{ fact1 }}"'},
                ]
            }
        ),
    )


test_remote = tosca.nodes.Root(
    "test_remote",
)
test_remote.configure = test_remote_configure


__all__ = ["test_remote"]
