# TESTS:
# _target
# f-strings: __str__ => "{{ 'expr' | eval }}"

import tosca
from typing import Optional

class Volume(tosca.nodes.Root):
    disk_label: str

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
                mountpoint = f"/mnt/{cls.volume_attachment._target.disk_label}",
            )
