# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT

import collections
from collections.abc import MutableSequence
import functools
import logging
import os
import sys
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, Union, cast

import ansible.constants as C
from ansible import context
from ansible.cli.playbook import PlaybookCLI
from ansible.plugins.callback.default import CallbackModule
from ansible.utils.display import Display

from tosca import ToscaInputs
from ..runtime import (
    CapabilityInstance,
    EntityInstance,
    HasInstancesInstance,
    NodeInstance,
    RelationshipInstance,
)
from ..projectpaths import FilePath, WorkFolder, get_path
from . import TemplateConfigurator, TemplateInputs
from ..configurator import ConfiguratorResult, Status, TaskView
from ..result import Result, serialize_value
from ..util import UnfurlTaskError, assert_form
from ..logs import getLogger
from ..support import reload_collections
from ..yamlloader import yaml

logger = getLogger("unfurl")

display = Display()


class AnsibleInputs(TemplateInputs):
    playbook: Union[None, str, List[dict]]
    inventory: Union[None, str, Dict[str, Any]] = None
    playbookArgs: Union[None, List[str]] = None
    resultKeys: Union[None, List[str]] = None


def get_ansible_results(result, extraKeys=(), facts=()) -> Tuple[Dict, Dict]:
    """
    Returns a dictionary containing at least:

    msg
    stdout
    returncode: (None if the process didn't complete)
    error: stderr or exception if one was raised
    **extraKeys
    """
    # result is per-task ansible.executor.task_result.TaskResult
    # https://github.com/ansible/ansible/blob/devel/lib/ansible/executor/task_result.py
    # https://docs.ansible.com/ansible/latest/reference_appendices/common_return_values.html
    # https://docs.ansible.com/ansible/latest/user_guide/playbooks_variables.html#variables-discovered-from-systems-facts

    # _check_key checks 'results' if task was a loop
    # 'warnings': result._check_key('warning'),
    result = result.clean_copy()
    if result.is_failed():
        logger.error("Playbook task failed: %s", result._result, extra=dict(truncate=0))
    else:
        logger.debug("Playbook task result: %s", result._result, extra=dict(truncate=0))
    resultDict = {}
    ignoredKeys = []
    # map keys in results to match the names that ShellConfigurator uses
    keyMap = {
        "returncode": "returncode",
        "msg": "msg",
        "exception": "error",
        "stdout": "stdout",
        "stderr": "stderr",
        "module_stdout": "stdout",
        "module_stderr": "stderr",
    }
    # other common keys: "result", "method", "diff", "invocation"
    for key in result._result:
        if key in keyMap:
            resultDict[keyMap[key]] = result._result[key]
        elif key in extraKeys:
            resultDict[key] = result._result[key]
        else:
            ignoredKeys.append(key)

    outputs = {}
    if facts:
        ansible_facts = result._result.get("ansible_facts")
        for fact in facts:
            if ansible_facts and fact in ansible_facts:
                outputs[fact] = ansible_facts[fact]
            elif fact in result._result:
                outputs[fact] = result._result[fact]
    resultDict["ignored"] = ignoredKeys
    return resultDict, outputs


def get_yaml_property(task: TaskView, prop_name: str, load_file: bool):
    prop_value = task.inputs.get(prop_name)
    if isinstance(prop_value, str):
        if "\n" in prop_value:
            return yaml.load(prop_value)
        else:
            if not os.path.isabs(prop_value):
                prop_value = get_path(task.inputs.context, prop_value, "src")
            if os.path.exists(prop_value):
                # set this as FilePath so we can monitor changes to it
                result = task.inputs._attributes[prop_name]
                if not isinstance(result, Result) or not result.external:
                    task.inputs[prop_name] = FilePath(prop_value)
                if load_file:
                    with open(prop_value) as f:
                        playbook_contents = f.read()
                    return yaml.load(playbook_contents)
                else:
                    return prop_value
            else:
                raise UnfurlTaskError(
                    task, f'Ansible {prop_name} "{prop_value}" does not exist'
                )
    return prop_value


class AnsibleConfigurator(TemplateConfigurator):
    """The task's target is the inventory.
    The template can inline a list of ansible tasks or supply a whole playbook.
    In either case, if the "hosts" key in playbook is missing or empty this configurator will choose the host based on the following criteria:
    * operation_host if present
    * the current target if it looks like a host (e.g. has an endpoint or is compute resource)
    * search the current target's hostedOn relationship for a node that looks like a host
    * localhost with a local connection

    If template doesn't supply its own Ansible inventory file as an input parameter, then generate a inventory file for the selected host.
    The inventory is built from the following sources:
    * the endpoint
    * the compute node's attributes
    * groups of type AnsibleInventoryGroup that the host node is a member of.
    * relationship templates (of type unfurl.relationships.ConnectsTo.Ansible) that target the endpoint.
    These can be set in the environment's "connection" section.
    """

    def __init__(self, *args: ToscaInputs, **kw) -> None:
        super().__init__(*args, **kw)
        self._cleanupRoutines: List[Callable] = []

    def can_dry_run(self, task):
        return True

    def _make_inventory_from_group(self, group, includeInstances):
        vars = {}
        children = {}
        # XXX:
        #  if includeInstances:
        #     for member in group.memberInstances:
        #         hosts[member.name] = self._getHostVars(member)
        for child in group.member_groups:
            if child.is_compatible_type("unfurl.groups.AnsibleInventoryGroup"):
                children[child] = self._make_inventory_from_group(
                    child, includeInstances
                )
        vars.update(group.properties.get("hostvars", {}))
        return dict(hosts={}, vars=vars, children=children)

    def _find_endpoint(self, node: NodeInstance) -> Optional[CapabilityInstance]:
        for connection in node.capabilities:
            if connection.template.is_compatible_type(
                "unfurl.capabilities.Endpoint.Ansible"
            ):
                return connection
        return None

    def _get_host_vars_from_endpoint(self, endpoint: CapabilityInstance):
        # return ansible_connection, ansible_host, ansible_user, ansible_port
        props = endpoint.attributes
        hostVars = {
            "ansible_" + name: props[name]
            for name in ("port", "host", "connection", "user")
            if name in props
        }
        hostVars.update(props.get("hostvars", {}))
        if "ansible_host" not in hostVars and hostVars.get("ip_address"):
            hostVars["ansible_host"] = hostVars["ip_address"]
        return hostVars

    def _get_host_vars_from_connection(self, connection: RelationshipInstance):
        hostVars = {}
        creds = connection.attributes.get("credential")
        if creds:
            if "user" in creds:
                hostVars["ansible_user"] = creds["user"]
            # e.g token_type is password or private_key_file:
            if "token" in creds:
                hostVars["ansible_" + creds["token_type"]] = creds["token"]
            if "keys" in creds:
                hostVars.update(creds["keys"])
        hostVars.update(connection.attributes.get("hostvars", {}))
        return hostVars

    def _is_host(self, host: NodeInstance):
        # XXX hackish
        return bool(
            host.attributes.get("public_ip") or host.attributes.get("private_ip")
        )

    def _make_inventory(
        self,
        host: Optional[NodeInstance],
        endpoint: Optional[CapabilityInstance],
        allVars: Dict[str, Any],
        task: TaskView,
    ) -> Tuple[Dict[str, Any], str]:
        # make a one-off ansible inventory for the current task
        # XXX add debug/verbose logging to indicate which was chosen
        hostVars = {}
        if host:
            # if a host node is provided build it from an Ansible SSH endpoint capability, the relationship targeting it and from attributes on the compute node itself
            if endpoint:
                hostVars = self._get_host_vars_from_endpoint(endpoint)
            connection = task.find_connection(
                task.inputs.context, host, "unfurl.relationships.ConnectsTo.Ansible"
            )
            if connection:
                hostVars.update(self._get_host_vars_from_connection(connection))
            if "ansible_host" not in hostVars:
                ip_address = host.attributes.get("public_ip") or host.attributes.get(
                    "private_ip"
                )
                if ip_address:
                    hostVars["ansible_host"] = ip_address
                    if "ansible_connection" not in hostVars:
                        hostVars["ansible_connection"] = "ssh"

            project_id = host.attributes.get("project_id")
            if project_id:
                # hack for gcp because this name is set in .ssh/config by `gcloud compute config-ssh --quiet`
                hostname = f"{host.name}.{host.attributes['zone']}.{project_id}"
            else:
                hostname = host.name
            hosts = {hostname: hostVars}

            children = {
                group.name: self._make_inventory_from_group(group, False)
                for group in host.template.groups
                if group.is_compatible_type("unfurl.groups.AnsibleInventoryGroup")
            }
        else:
            # no host instance targeted, default to localhost
            hostname = "localhost"
            hosts = {hostname: hostVars}
            children = {}

        if (
            hostname == "localhost" or hostVars.get("ansible_connection") == "local"
        ) and "ansible_python_interpreter" not in hostVars:
            # we need to set this in case we are running inside a Python virtual environment, see:
            # https://docs.ansible.com/ansible/latest/scenario_guides/guide_rax.html#running-from-a-python-virtual-environment-optional
            hostVars["ansible_python_interpreter"] = sys.executable

        # note: allVars is inventory vars shared by all hosts
        return dict(all=dict(hosts=hosts, vars=allVars, children=children)), hostname

    def _find_host(
        self, task: TaskView
    ) -> Tuple[Optional[NodeInstance], Optional[CapabilityInstance]]:
        if task.configSpec.operation_host:  # explicitly set
            if isinstance(task.operation_host, NodeInstance):
                return task.operation_host, self._find_endpoint(task.operation_host)
            return None, None  # it must be set to the root, use localhost
        if isinstance(task.target, NodeInstance):
            endpoint = self._find_endpoint(task.target)
            if endpoint or self._is_host(task.target):
                return task.target, endpoint
            else:
                for host in task.target.hosted_on:
                    endpoint = self._find_endpoint(task.target)
                    if endpoint or self._is_host(host):
                        return host, endpoint
        return None, None

    def get_inventory(self, task: TaskView, cwd: WorkFolder) -> Tuple[str, str]:
        inventory = get_yaml_property(task, "inventory", False)
        if inventory and isinstance(inventory, str):
            # XXX if user set inventory file we can create a folder to merge them
            # https://allandenot.com/devops/2015/01/16/ansible-with-multiple-inventory-files.html
            return inventory, ""  # assume its a file path
        if not inventory:
            # XXX merge inventory
            host, endpoint = self._find_host(task)
            inventory, hostname = self._make_inventory(
                host, endpoint, inventory or {}, task
            )
        # XXX cache and reuse
        return cwd.write_file(serialize_value(inventory), "inventory.yml"), hostname
        # don't worry about the warnings in log, see:
        # https://github.com/ansible/ansible/issues/33132#issuecomment-346575458
        # https://github.com/ansible/ansible/issues/33132#issuecomment-363908285
        # https://github.com/ansible/ansible/issues/48859

    def _cleanup(self):
        for func in self._cleanupRoutines:
            try:
                func()
            except:
                # XXX: log
                pass
        self._cleanupRoutines = []

    def get_vars(self, task):
        vars = task.inputs.context.vars.copy()
        vars["__unfurl"] = task.inputs.context
        return vars

    def _make_play(self, play, task: TaskView, host: str):
        if not play.get("hosts"):
            if not host:
                raise UnfurlTaskError(
                    task,
                    f"'hosts' missing from playbook but inventory was user specified.",
                )
            # XXX default to host group instead of localhost depending on operation_host?
            # host is the name of the host set by _make_inventory
            play["hosts"] = host
            if host == "localhost":
                play["connection"] = "local"
                play["gather_facts"] = False
        return play

    def _make_playbook(self, playbook, task: TaskView, host: str):
        if (
            not playbook
            or not isinstance(playbook, MutableSequence)
            or not isinstance(playbook[0], Mapping)
        ):
            raise UnfurlTaskError(task, f"malformed playbook: {playbook}")
        if "tasks" not in playbook[0]:
            play = dict(tasks=playbook)
            return [self._make_play(play, task, host)]
        else:
            return [self._make_play(play, task, host) for play in playbook]

    def find_playbook(self, task: TaskView):
        return get_yaml_property(task, "playbook", True)

    def get_playbook(self, task: TaskView, cwd: WorkFolder, host: str):
        playbook = self.find_playbook(task)
        if isinstance(playbook, str):
            # assume it's file path
            return playbook
        playbook = self._make_playbook(playbook, task, host)
        envvars = task.get_environment(True)
        for play in playbook:
            play["environment"] = envvars
        return cwd.write_file(serialize_value(playbook), "playbook.yml")

    def get_playbook_args(self, task: TaskView):
        args = task.inputs.get("playbookArgs", [])
        if not isinstance(args, MutableSequence):
            args = [args]
        if task.dry_run:
            args.append("--check")
        if task.configSpec.timeout:
            args.append(f"--timeout={task.configSpec.timeout}")
        if task.verbose > 0:
            args.append("-" + ("v" * task.verbose))
        return list(args)

    def get_result_keys(self, task, results):
        return task.inputs.get("resultKeys", [])

    def process_result(self, task: TaskView, result: ConfiguratorResult) -> ConfiguratorResult:
        errors, status = self.process_result_template(
            task, dict(cast(dict, result.result), success=result.success, outputs=result.outputs)
        )
        result.success = not errors
        return result

    def render(self, task):
        cwd = task.set_work_folder()
        # build host inventory from resource
        inventory, hostname = self.get_inventory(task, cwd)
        playbook = self.get_playbook(task, cwd, hostname)
        playbookArgs = self.get_playbook_args(task)
        args = _render_playbook(playbook, inventory, playbookArgs)
        return args

    def run(self, task):
        try:
            args = task.rendered
            # build vars from inputs
            extraVars = self.get_vars(task)
            extraVars.update(task.inputs.get_copy("arguments", {}))
            if task.operation_host and task.operation_host.templar:
                vault_secrets = task.operation_host.templar._loader._vault.secrets
            else:
                vault_secrets = None
            resultCallback = _run_playbooks(
                args,
                extraVars,
                vault_secrets,
            )

            if resultCallback.exit_code or len(resultCallback.resultsByStatus.failed):
                status = Status.error
            else:
                # unreachable, failed, skipped
                # XXX degraded vs. error if required?
                status = Status.ok

            logger.debug(
                "ran playbook: status %s changed %s, total %s ",
                status,
                resultCallback.changed,
                len(resultCallback.results),
            )

            if resultCallback.results:
                resultKeys = self.get_result_keys(task, resultCallback.results)
                factKeys = list(task.configSpec.outputs)
                # each task in a playbook will have a corresponding result
                resultList, outputList = zip(
                    *map(
                        lambda result: get_ansible_results(
                            result, resultKeys, factKeys
                        ),
                        resultCallback.results,
                    )
                )
                mergeFn = lambda a, b: a.update(b) or a
                results: Dict = functools.reduce(mergeFn, resultList, {})
                outputs: Dict = functools.reduce(mergeFn, outputList, {})
            else:
                results, outputs = {}, {}

            targetStatus = None
            if resultCallback.changed > 0:
                modified = True
                if any(
                    r.is_failed() and r.is_changed() for r in resultCallback.results
                ):
                    targetStatus = Status.error
            else:
                modified = False

            success = status == Status.ok and not task._errors
            result = self.done(
                task,
                success=success,
                status=targetStatus,
                modified=modified,
                result=results,
                outputs=outputs,
            )
            if (
                (results or outputs)
                and status == Status.ok
                or status == Status.degraded
            ):
                # this can update resources so don't do it on error
                # XXX align with shell configurator behavior
                # (which is called unconditionally and before done())
                result = self.process_result(task, result)
            yield result

        finally:
            self._cleanup()


# see https://github.com/ansible/ansible/blob/d72587084b4c43746cdb13abb262acf920079865/examples/scripts/uptime.py
# and https://github.com/ansible/ansible/blob/devel/lib/ansible/plugins/callback/log_plays.py
_ResultsByStatus = collections.namedtuple(
    "_ResultsByStatus", "ok failed skipped unreachable"
)


class ResultCallback(CallbackModule):
    # NOTE: callbacks will run in separate process
    # see ansible.executor.task_result.TaskResult and ansible.playbook.task.Task

    def __init__(self):
        super().__init__()
        self.results = []
        # named tuple of OrderedDict<task_name:list<result>>
        self.resultsByStatus = _ResultsByStatus(
            *[collections.OrderedDict() for x in range(4)]
        )
        self._load_name = "result"
        self.changed = 0

    def get_option(self, k):
        return False

    def get_info(self, result):
        host = result._host
        taskname = result.task_name
        fields = result._task_fields.keys()
        keys = result._result.keys()
        return f"{host}: {taskname}({fields}) => {keys}"

    def _add_result(self, status, result):
        self.results.append(result)
        if result._result.get("changed", False):
            self.changed += 1
        # XXX should save by host too
        getattr(self.resultsByStatus, status).setdefault(result.task_name, []).append(
            result
        )

    def v2_runner_on_ok(self, result):
        self._add_result("ok", result)
        # print("ok", self.getInfo(result))
        super().v2_runner_on_ok(result)

    def v2_runner_on_skipped(self, result):
        self._add_result("skipped", result)
        # print("skipped", self.getInfo(result))
        super().v2_runner_on_skipped(result)

    def v2_runner_on_failed(self, result, **kwargs):
        self._add_result("failed", result)
        # print("failed", self.getInfo(result))
        super().v2_runner_on_failed(result, **kwargs)

    def v2_runner_on_unreachable(self, result):
        self._add_result("unreachable", result)
        # print("unreachable", self.getInfo(result))
        super().v2_runner_on_unreachable(result)


def _init_global_context(options):
    # Context _init_global_context is designed to be called only once,
    # setting CLIARGS to an ImmutableDict singleton
    # Since we need to change it, replace the function with one that hacks into CLIARGS internal state
    context.CLIARGS._store = vars(options)


def run_playbooks(playbook, _inventory, params=None, args=None):
    args = _render_playbook(playbook, _inventory, args)
    return _run_playbooks(args, params)


def _render_playbook(playbook, _inventory, args):
    inventoryArgs = ["-i", _inventory] if _inventory else []
    args = ["ansible-playbook"] + inventoryArgs + (args or []) + [playbook]
    return args


def _run_playbooks(args, params=None, vault_secrets=None):
    logger.info("running " + " ".join(args))
    reload_collections()
    cli = PlaybookCLI(args)

    context._init_global_context = _init_global_context
    cli.parse()

    # replace C.DEFAULT_STDOUT_CALLBACK with our own so we have control over logging
    # config/base.yml sets C.DEFAULT_STDOUT_CALLBACK == 'default' (ansible/plugins/callback/default.py)
    # (cli/console.py and cli/adhoc.py sets it to 'minimal' but PlaybookCLI.run() in cli/playbook.py uses the default)
    # see also https://github.com/projectatomic/atomic-host-tests/blob/master/callback_plugins/default.py
    resultsCB = ResultCallback()
    resultsCB.set_options()
    C.DEFAULT_STDOUT_CALLBACK = resultsCB

    _play_prereqs = cli._play_prereqs

    def hook_play_prereqs():
        loader, inventory, variable_manager = _play_prereqs()
        if vault_secrets:
            loader.set_vault_secrets(vault_secrets)
        if params:
            variable_manager._extra_vars.update(params)
        resultsCB.inventoryManager = inventory
        resultsCB.variableManager = variable_manager
        return loader, inventory, variable_manager

    cli._play_prereqs = hook_play_prereqs

    oldVerbosity = display.verbosity
    if logger.getEffectiveLevel() <= logging.DEBUG:
        display.verbosity = 2
    try:
        resultsCB.exit_code = cli.run()
    finally:
        display.verbosity = oldVerbosity
    return resultsCB
