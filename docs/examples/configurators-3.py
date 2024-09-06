@operation(name="configure", operation_host="staging.example.com")
def test_remote_configure(**kw):
    return unfurl.configurators.CmdConfigurator(
        cmd='echo "test"',
    )


test_remote = tosca.nodes.Root(
    "test_remote",
)
test_remote.configure = test_remote_configure


__all__ = ["test_remote"]


