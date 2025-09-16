import unfurl
import tosca
from tosca import ToscaOutputs, Attribute, Eval


class MyArtifact(unfurl.artifacts.ShellExecutable):
    file: str = "myscript.sh"

    # evaluates the script's output
    outputsTemplate = Eval("{{ stdout | from_json }}")

    class Outputs(ToscaOutputs):
        output1: str = Attribute()
        output2: int = Attribute()

    # no implementation is needed for execute() because:
    # - execute() arguments are passed as command line arguments by default
    # - MyArtifact.Outputs is constructed from the json map returned by outputsTemplate
    def execute(self, arg1: str, arg2: int) -> Outputs:
        return MyArtifact.Outputs()


class MyNode(tosca.nodes.Root):
    prop: int

    def configure(self) -> MyArtifact.Outputs:
        return MyArtifact(input="y").execute("hello", self.prop)


my_node = MyNode(prop=1)
