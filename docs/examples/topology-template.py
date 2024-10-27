from tosca import Eval, TopologyInputs, TopologyOutputs


class Inputs(TopologyInputs):
    domain: str = "example.com"


class Outputs(TopologyOutputs):
    url: str = Eval(
        "https://{{ TOPOLOGY.inputs.domain }}:{{ NODES.myApp.portspec.source }}/api/events"
    )
