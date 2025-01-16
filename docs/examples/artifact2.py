import unfurl
import tosca
from tosca import ToscaOutputs, Attribute, Eval
class MyArtifact(unfurl.artifacts.ShellExecutable):
    file: str = "myscript.sh"
    contrived_key: str
    outputsTemplate = Eval("{{ stdout | from_json | subelements(SELF.contrived_key)}}")

    class Outputs(ToscaOutputs):
        a_output: str = Attribute()

    def execute(self, arg1: str, arg2: int) -> Outputs:
        return MyArtifact.Outputs()

class MyNode(tosca.nodes.Root):
    prop1: str
    prop2: int

    def configure(self) -> MyArtifact.Outputs:
        return MyArtifact(contrived_key=self.prop1).execute("hello", arg2=self.prop2) 

my_node = MyNode(prop1="foo", prop2=1)
