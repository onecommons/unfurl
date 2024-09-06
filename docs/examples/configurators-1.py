@operation(name="configure")
def test_remote_configure(**kw):
  return unfurl.configurators.shell.ShellConfigurator(
    command='echo "abbreviated configuration"',
  )


test_remote = tosca.nodes.Root(
    "test_remote",
)
test_remote.configure = test_remote_configure


__all__ = ["test_remote"]
