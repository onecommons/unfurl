import unfurl
import tosca
from typing import Any, Callable, List, Optional
from tosca import *
from unfurl.configurator import TaskView
from unfurl.configurators import DoneDict, TemplateConfigurator, TemplateInputs

class Test(tosca.nodes.Root):
    url_scheme: str
    host: str
    computed: str = tosca.Attribute()
    data: "MyDataType" = tosca.DEFAULT
    int_list: List[int] = tosca.DEFAULT
    data_list: List["MyDataType"] = tosca.DEFAULT
    a_requirement: unfurl.nodes.Generic

    @tosca.computed(
        title="URL",
        metadata={"sensitive": True},
    )
    def url(self) -> str:
        return f"{ self.url_scheme }://{self.host }"

    def run(self, task: TaskView) -> bool:
        DoneDict()  # this is from an unsafe module, test that its available in this context
        self.computed = self.url
        self.data.ports.source = tosca.datatypes.NetworkPortDef(80)
        self.data.ports.target = tosca.datatypes.NetworkPortDef(8080)
        self.int_list.append(1)
        self.int_list.append(2)
        del self.int_list[1]
        extra = self.a_requirement.extra  # type: ignore
        self.a_requirement.copy_of_extra = extra  # type: ignore

        self.data_list.append(MyDataType())
        self.data_list[0].ports.source = tosca.datatypes.NetworkPortDef(80)
        self.data_list[0].ports.target = tosca.datatypes.NetworkPortDef(8080)
        return True

    def create(self, **kw: Any) -> Callable[[Any], Any]:
        return self.run

    @operation(outputs=dict(test_output="computed"))
    def delete(self, **kw: Any) -> TemplateConfigurator:
        render = self._context  # type: ignore
        done=DoneDict(outputs=dict(test_output="set output"))
        return TemplateConfigurator(TemplateInputs(run="test me", done=done))

class MyDataType(tosca.DataType):
    ports: tosca.datatypes.NetworkPortSpec = tosca.Property(
                factory=lambda: tosca.datatypes.NetworkPortSpec(**tosca.PortSpec.make(80))
              )

generic = unfurl.nodes.Generic("generic")
generic.extra = "extra"  # type: ignore
test = Test(url_scheme="https", host="foo.com", a_requirement=generic)

if __name__ == "__main__":
    print(test)
    print(tosca.nodes.Root())