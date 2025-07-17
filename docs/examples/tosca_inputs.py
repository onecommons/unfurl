import unfurl
import tosca
class Base(tosca.nodes.Root):

    class Inputs(tosca.ToscaInputs):
      foo: str
      bar: str = ""

    # attributes named "_<interface_name>_default_inputs" set the inputs 
    # that are applied to all operations in that interface
    _Standard_default_inputs = Inputs(foo="base:default")

    def create(self, foo: str = "base:create"):
        ...
        
class Derived(Base):

    _Standard_default_inputs = Base.Inputs(foo="derived:default", bar="derived:default")

    def create(self, foo: str= "base:create", bar: str = "derived:create"):
        ...

example = Derived()
