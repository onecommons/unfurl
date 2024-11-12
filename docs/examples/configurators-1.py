@operation(name="configure")
def test_remote_configure(self, **kw):
    return unfurl.configurators.shell.ShellConfigurator(
        command='echo "abbreviated configuration"',
    )

test_remote = tosca.nodes.Root()
test_remote.set_operation(test_remote_configure)
