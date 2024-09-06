@operation(name="configure")
def shellscript_example_configure(**kw):
    return unfurl.configurators.shell.ShellConfigurator(
        command='if ! [ -x "$(command -v testvars)" ]; then\n  source testvars.sh\nfi\n',
        cwd=Eval('{{ "project" | get_dir }}'),
        keeplines=True,
        shell=Eval('{{ "bash" | which }}'),
    )


shellscript_example = tosca.nodes.Root(
    "shellscript-example",
)
shellscript_example.configure = shellscript_example_configure


__all__ = ["shellscript_example"]

