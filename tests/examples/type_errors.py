import unfurl
import tosca
from tosca import gb, Size
from typing_extensions import reveal_type
class ContainerService(tosca.nodes.Root):
    image: str
    url: str
    mem_size: tosca.Size
    name: str = tosca.CONSTRAINED

    @classmethod
    def _class_init(cls) -> None:
        # the proxy's backend is set to the container's url
        g: Size = 2 * gb
        bar: Size = g * 2.0
        baz: Size = g * 2
        wrong = g * "ddd"
        change_unit: Size = g * tosca.MB
        mismatch = g * 2.0 * tosca.kHz
        mismatch = g * tosca.HZ
        tosca.in_range(2*gb*2, 2*gb*2).apply_constraint(cls.mem_size)
        tosca.in_range(2*gb*2, 2*gb*2).apply_constraint(cls.url)
        baz + 50*tosca.GHz
        tosca.MB.as_int(baz)
        tosca.GHz.as_int(baz)
