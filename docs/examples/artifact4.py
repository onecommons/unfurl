import unfurl
import tosca


class AbstractArtifact(unfurl.artifacts.Executable):
    pass


class MyArtifact(AbstractArtifact):
    def execute(self):
        return self.do_default_stuff

    def do_default_stuff(self, task):
        return True


class AWSArtifact(AbstractArtifact):
    _interface_requirements = ["unfurl.relationships.ConnectsTo.AWSAccount"]

    def do_aws_stuff(self, task):
        return True

    def execute(self):
        return self.do_aws_stuff


aws_implementation = AWSArtifact()

default_implementation = MyArtifact()


class MyNode(tosca.nodes.Root):
    
    def configure(self):
        # Because this is an abstract artifact (its execute operation is not implemented), 
        # the generated YAML will only include the artifact type in the operation's implementation
        return AbstractArtifact() 


my_template = MyNode()
