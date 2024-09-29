import unfurl
import tosca
from typing import Any, Callable, List, Optional
from tosca import *
from unfurl.configurator import TaskView
from unfurl.configurators import DoneDict, TemplateConfigurator, TemplateInputs

class Base(tosca.nodes.Root):
    url: str  # test that a derived class safely override a property with a computed property


port80 = tosca.datatypes.NetworkPortDef(80)
class Test(Base):
    url_scheme: str
    host: str
    computed: str = tosca.Attribute()
    data: "MyDataType" = tosca.DEFAULT
    int_list: List[int] = tosca.DEFAULT
    data_list: List["MyDataType"] = tosca.DEFAULT
    a_requirement: unfurl.nodes.Generic

    def _url(self) -> str:
        return f"{ self.url_scheme }://{self.host }"

    url: str = tosca.Computed(factory=_url)

    def run(self, task: TaskView) -> bool:
        DoneDict()  # this is from an unsafe module, test that its available in this context
        self.computed = self.url
        self.data.ports.source = port80
        self.data.ports.target = tosca.datatypes.NetworkPortDef(8080)
        self.int_list.append(1)
        self.int_list.append(2)
        del self.int_list[1]
        extra = self.a_requirement.extra  # type: ignore
        self.a_requirement.copy_of_extra = extra  # type: ignore

        self.data_list.append(MyDataType().extend(additional=1))
        self.data_list[0].ports.source = port80
        self.data_list[0].ports.target = tosca.datatypes.NetworkPortDef(8080)
        return True

    def create(self, **kw: Any) -> Callable[[Any], Any]:
        return self.run

    @operation(outputs=dict(test_output="computed"))
    def delete(self, **kw: Any) -> TemplateConfigurator:
        render = self._context  # type: ignore
        done = DoneDict(outputs=dict(test_output=Eval("{{'set output'}}")))
        return TemplateConfigurator(TemplateInputs(run="test me", done=done))


class MyDataType(tosca.OpenDataType):
    ports: tosca.datatypes.NetworkPortSpec = tosca.Property(
        factory=lambda: tosca.datatypes.NetworkPortSpec(source=port80, target=port80)
    )

generic = unfurl.nodes.Generic("generic")
assert generic._name == "generic"
setattr(generic, "extra", "extra")
assert getattr(generic, "extra") == "extra"
test = Test(url_scheme="https", host="foo.com", a_requirement=generic)

tosca.global_state.mode = "runtime"
# unevaluated expression:
assert test.url == {
    "eval": {"computed": "service_template.mytypes:Test._url"}
}, test.url

# string conversions always convert to jinja2 expressions so f-string work:
assert (
    str(test.url)
    == "{{ {'eval': {'computed': 'service_template.mytypes:Test._url'}} | map_value }}"
), str(test.url)

if __name__ == "__main__":
    print(generic)
    print(tosca.nodes.Root())
