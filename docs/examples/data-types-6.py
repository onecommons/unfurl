import tosca
class Data1(tosca.datatypes.Root):
  prop1: str = "prop1_default"
  prop2: str = "prop2_default"
  prop3: str = "prop3_default"

class MyApp(tosca.nodes.Root):
    data2: Data1 = Data1(prop2="prop2_override")

class DerivedFromMyApp(MyApp):
    data2: Data1 = Data1(prop3="prop3_override")

my_app = DerivedFromMyApp("my_app")
