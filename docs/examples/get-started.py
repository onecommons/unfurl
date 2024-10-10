import unfurl
from typing import List, Dict, Any, Tuple, Union, Sequence
import tosca
from tosca import Eval, GB, MB, operation
import unfurl.configurators.shell


@operation(name="check")
def my_server_check(**kw):
    return unfurl.configurators.shell.ShellConfigurator(
        command=Eval(
            "gcloud compute instances describe {{ '.name' | eval }}  --format=json"
        ),
        resultTemplate=Eval(
            {
                "eval": {
                    "if": "$result",
                    "then": {
                        "readyState": {
                            "state": "{{ {'PROVISIONING': "
                            "'creating', 'STAGING': "
                            "'starting', 'RUNNING': "
                            "'started', 'REPAIRING' "
                            "'error,' 'SUSPENDING': "
                            "'stopping',  'SUSPENDED': "
                            "'stopped', 'STOPPING': "
                            "'deleting', 'TERMINATED': "
                            "'deleted'}[result.status] }}",
                            "local": "{{ {'PROVISIONING': 'pending', "
                            "'STAGING': 'pending', "
                            "'RUNNING': 'ok', 'REPAIRING' "
                            "'error,' 'SUSPENDING': "
                            "'error',  'SUSPENDED': "
                            "'error', 'STOPPING': 'absent', "
                            "'TERMINATED': "
                            "'absent'}[result.status] }}",
                        }
                    },
                },
                "vars": {
                    "result": "{%if success %}{{ stdout | from_json }}{% endif %}"
                },
            }
        ),
    )


@operation(name="delete")
def my_server_delete(**kw):
    return unfurl.configurators.shell.ShellConfigurator(
        command=Eval("gcloud compute instances delete {{ '.name' | eval }}"),
    )


@operation(name="create")
def my_server_create(**kw):
    return unfurl.configurators.shell.ShellConfigurator(
        command=Eval(
            (
                "gcloud compute instances create {{ '.name' | eval }}\n"
                '--boot-disk-size={{ {"get_property": ["SELF", "host", "disk_size"]} | eval | '
                'regex_replace(" ") }}\n'
                "--image=$(gcloud compute images list --filter=name:{{ {'get_property': "
                "['SELF', 'os', 'distribution']} | eval }}\n"
                "      --filter=name:focal --limit=1 --uri)\n"
                "--machine-type=e2-medium   > /dev/null\n"
                "&& gcloud compute instances describe {{ '.name' | eval }} --format=json\n"
            )
        ),
        resultTemplate=Eval(
            {
                "eval": {
                    "then": {
                        "attributes": {
                            "public_ip": "{{ "
                            "result.networkInterfaces[0].accessConfigs[0].natIP "
                            "}}",
                            "private_ip": "{{ "
                            "result.networkInterfaces[0].networkIP "
                            "}}",
                            "zone": "{{ result.zone | basename }}",
                            "id": "{{ result.selfLink }}",
                        }
                    }
                }
            }
        ),
    )


my_server = tosca.nodes.Compute(
    "my_server",
    host=tosca.capabilities.Compute(
        num_cpus=1,
        disk_size=200 * GB,
        mem_size=512 * MB,
    ),
    os=tosca.capabilities.OperatingSystem(
        architecture="x86_64",
        type="linux",
        distribution="ubuntu",
    ),
)
my_server.check = my_server_check
my_server.delete = my_server_delete
my_server.create = my_server_create
