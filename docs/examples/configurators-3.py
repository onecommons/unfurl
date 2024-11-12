import unfurl.configurators
import tosca
@tosca.operation(name="configure", operation_host="staging.example.com")
def test_remote_configure(self, **kw):
    return unfurl.configurators.CmdConfigurator(
        cmd='echo "test"',
    )


test_remote = tosca.nodes.Root()
test_remote.set_operation(test_remote_configure)
