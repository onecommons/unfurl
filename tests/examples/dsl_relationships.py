# TESTS:
# _target
# f-strings: __str__ => "{{ 'expr' | eval }}"
# unfurl.tosca_plugins imports in safe mode
# runtime_func in safe mode

import unfurl
import tosca
from typing import Optional
from unfurl.tosca_plugins.expr import runtime_func
from unfurl.tosca_plugins.functions import to_dns_label

@runtime_func
def disk_label(label: str) -> str:
    return to_dns_label(label + "_disk")

class Volume(tosca.nodes.Root):
    disk_label: str
    disk_size: tosca.Size = 100 * tosca.GB

class VolumeAttachment(tosca.relationships.AttachesTo):
    _target: Volume

class VolumeMountArtifact(tosca.artifacts.Root):
    mountpoint: str

class TestTarget(tosca.nodes.Root):
    volume_mount: VolumeMountArtifact = tosca.CONSTRAINED
    volume_attachment: Optional[VolumeAttachment]

    @classmethod
    def _class_init(cls):
        if cls.volume_attachment:  # this always evaluates to true, only for type checking
            cls.volume_mount = VolumeMountArtifact(
                file="",
                # XXX intent = when_expr(cls.volume_attachment, 'mount', 'skip')
                intent = 'mount' if cls.volume_attachment else 'skip',
                target = "HOST",
                mountpoint = f"/mnt/{disk_label(cls.volume_attachment._target.disk_label)}",
            )
