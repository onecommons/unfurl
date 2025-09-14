import unfurl
import tosca
from tosca import ToscaOutputs, Attribute, Eval
class MyArtifact(unfurl.artifacts.ShellExecutable):
    file: str = "myscript.sh"
    contrived_key: str
    # evaluates to a dictionary of outputs
    outputsTemplate = Eval("{{ stdout | from_json }}")

    class Outputs(ToscaOutputs):
        a_output: str = Attribute()

    def run(self, task):
        o = MyArtifact.Outputs()
        args = task.inputs.get_copy("arguments")
        o.a_output = f"{args['arg1']}{args['arg2']}"
        return o

    def execute(self, arg1: str, arg2: int) -> Outputs:
        command = f"""echo '{{"a_output": "{arg1}{arg2}"}}'"""
        %s  # see test_artifact.py::test_artifact_execute
        return MyArtifact.Outputs()  # outputsTemplate convert the output json should conform to MyArtifact.Outputs


class MyNode(tosca.nodes.Root):
    prop1: str
    prop2: int

    def configure(self) -> MyArtifact.Outputs:
        return MyArtifact(contrived_key=self.prop1).execute("hello", arg2=%s)

my_node = MyNode(prop1="foo", prop2=1)
