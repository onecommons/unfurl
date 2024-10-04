from tosca import TopologyInputs

class Inputs(TopologyInputs):
    image_name: str = "Ubuntu 12.04"

vm = MyVM(image_name=Inputs.image_name)

