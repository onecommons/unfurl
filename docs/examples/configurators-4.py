@operation(name="configure")
def shellscript_example_configure(self, **kw):
    return unfurl.configurators.shell.ShellConfigurator(
        command='if ! [ -x "$(command -v testvars)" ]; then\n  source testvars.sh\nfi\n',
        cwd=Eval('{{ "project" | get_dir }}'),
        keeplines=True,
        shell=Eval('{{ "bash" | which }}'),
    )

shellscript_example = tosca.nodes.Root()
shellscript_example.set_operation(shellscript_example_configure)

