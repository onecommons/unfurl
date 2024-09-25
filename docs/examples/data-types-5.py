class Data1(tosca.datatypes.Root):
    prop1: str = "prop1_default"
    prop2: str = "prop2_default"
    prop3: str = "prop3_default"

class MyApp(tosca.nodes.Root):
    data1: Data1 = Data1(prop2="prop2_override")

my_app = MyApp(
    "my_app",
    data1=Data1(
        prop3="prop3_override"
    )
)

__all__ = ["Data1", "MyApp"]

