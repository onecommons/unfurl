import unfurl
import tosca
from typing import Any, Callable, List, Optional
from tosca import *
from unfurl.configurator import TaskView

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
        self.computed = self.url
        self.data.ports.source = tosca.datatypes.NetworkPortDef(80)
        self.data.ports.target = tosca.datatypes.NetworkPortDef(8080)
        self.int_list.append(1)
        extra = self.a_requirement.extra  # type: ignore
        self.a_requirement.copy_of_extra = extra  # type: ignore

        # XXX make this work:
        # self.data_list.append(MyDataType())
        # self.data_list[0].ports.source = 80
        # self.data_list[0].ports.target = 8080
        return True

    def create(self, **kw: Any) -> Callable[[Any], Any]:
        return self.run

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