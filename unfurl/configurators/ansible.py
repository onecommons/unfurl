from __future__ import absolute_import
import collections
from ..util import ansibleDisplay, ansibleDummyCli, assertForm, saveToTempfile
from ..configurator import Configurator, Status
from ..result import serializeValue
import ansible.constants as C
from ansible.cli.playbook import PlaybookCLI
from ansible.plugins.callback.default import CallbackModule
from ansible.module_utils import six
import logging

logger = logging.getLogger("unfurl")

# input parameters:
#  playbook
#  playbookArgs
#  extraVars
#  inventory
#  facts (list of ansible facts to extract from ansible results)
def ansibleResults(result, extraKeys=()):
    # result is per-task ansible.executor.task_result.TaskResult
    # https://github.com/ansible/ansible/blob/devel/lib/ansible/executor/task_result.py
    # https://docs.ansible.com/ansible/latest/reference_appendices/common_return_values.html
    # map to same result names used by shellconfigurator

    # XXX map ansible facts and user set variables, needed for result templates
    # https://docs.ansible.com/ansible/latest/user_guide/playbooks_variables.html#variables-discovered-from-systems-facts
    """
  Returns a dictionary containing at least:

  stderr
  stdout
  returncode (None if the process didn't complete)
  error if an exception was raised
  """
    # _check_key checks 'results' if task was a loop
    # 'warnings': result._check_key('warning'),
    result = result.clean_copy()
    # print('result._result', result._result)
    resultDict = {"returncode": result._check_key("rc")}
    keyMap = {
        "msg": ["msg"],
        "error": ["exception", "module_stderr"],
        "stdout": ["stdout", "module_stdout"],
    }
    for name, keys in keyMap.items():
        for key in keys:
            val = result._check_key(key)
            if val:
                resultDict[name] = val
                break

    for key in extraKeys:
        resultDict[key] = result._check_key(key)
    return resultDict


class AnsibleConfigurator(Configurator):
    """
  The current resource is the inventory.
  #could have parameter for mapping resource attributes to groups
  #also need to map attributes to host vars
  sshconfig
  ansible variables can not be set to a value of type resource

  external inventory discovers resources
  need away to map hosts to existing resources
  and to map vars to types of different resource
  """

    def __init__(self, configSpec):
        super(AnsibleConfigurator, self).__init__(configSpec)
        self._cleanupRoutines = []

    def canDryRun(self, task):
        return True

    def _makeInventoryFromGroup(self, group):
        hosts = {}
        vars = {}
        children = {}
        for member in group.memberInstances:
            hosts[member.name] = self._getHostVars(member)
        for child in group.memberGroups:
            if child.isCompatibleType("unfurl.groups.AnsibleInventoryGroup"):
                children[child] = self._makeInventoryFromGroup(child)
        for prop, value in group.properties.items():
            vars[prop] = value
        return dict(hosts=hosts, vars=vars, children=children)

    def _getHostVars(self, node):
        # return ansible_connection, ansible_host, ansible_user, ansible_port
        connections = node.getCapabilities("endpoint")
        for connection in connections:
            if connection.template.isCompatibleType(
                "unfurl.capabilities.Endpoint.Ansible"
            ):
                break
        else:
            return {}
        props = connection.attributes
        hostVars = {
            "ansible_" + name: props[name]
            for name in ("port", "host", "connection")
            if name in props
        }
        creds = connection.attributes.get("credential")
        if creds:
            if "user" in creds:
                hostVars["ansible_user"] = creds["user"]
                # e.g token_type is password or private_key_file:
                hostVars["ansible_" + creds["token_type"]] = creds["token"]
                if "keys" in creds:
                    hostVars.update(creds["keys"])
        hostVars.update(props.get("hostvars", {}))
        if "ansible_host" not in hostVars and hostVars.get("ip_address"):
            hostVars["ansible_host"] = hostVars["ip_address"]
        return hostVars

    def _makeInventory(self, host, allVars):
        hosts = {host.name: self._getHostVars(host)}
        children = {
            group.name: self._makeInventoryFromGroup(group)
            for group in host.template.getGroups()
            if group.isCompatibleType("unfurl.groups.AnsibleInventoryGroup")
        }
        # allVars is inventory vars shared by all hosts
        return dict(all=dict(hosts=hosts, vars=allVars, children=children))

    def getInventory(self, task):
        inventory = task.inputs.get("inventory")
        if inventory and isinstance(inventory, six.string_types):
            # XXX if user set inventory file we can create a folder to merge them
            # https://allandenot.com/devops/2015/01/16/ansible-with-multiple-inventory-files.html
            return inventory  # assume its a file path

        if not inventory:
            # XXX merge inventory
            host = task.operationHost
            if not host:
                return inventory  # default to localhost if not inventory
            inventory = self._makeInventory(host, inventory or {})
        # XXX cache and reuse file
        return saveToTempfile(inventory, "-inventory.yaml").name
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

    # have a parameter for the mapping or just set_facts in playbook
    def getVars(self, task):
        """
    just add a lookup() plugin?
    add attribute to parameters to map to vars?
    # ansible yaml file but we evaluate the valuesFrom first?
    """
        return {}

    def _makePlayBook(self, playbook, task):
        assertForm(playbook, collections.MutableSequence)
        # XXX use host group instead of localhost depending on operation_host
        hosts = task.operationHost and task.operationHost.name or "localhost"
        if playbook and not "hosts" in playbook[0]:
            play = dict(hosts=hosts, gather_facts=False, tasks=playbook)
            if hosts == "localhost":
                play["connection"] = "local"
            return [play]
        else:
            return playbook

    def findPlaybook(self, task):
        return task.inputs["playbook"]

    def getPlaybook(self, task):
        playbook = self.findPlaybook(task)
        if isinstance(playbook, six.string_types):
            # assume it's file path
            return playbook
        playbook = self._makePlayBook(playbook, task)
        envvars = task.environ
        for play in playbook:
            play["environment"] = envvars
        return saveToTempfile(serializeValue(playbook), "-playbook.yml").name

    def getPlaybookArgs(self, task):
        args = task.inputs.get("playbookArgs", [])
        if not isinstance(args, collections.MutableSequence):
            args = [args]
        if task.dryRun:
            args.append("--check")
        return args

    def _processResult(self, task, result):
        resultTemplate = task.inputs.get("resultTemplate")
        if resultTemplate:
            results = task.query(
                {"eval": dict(template=resultTemplate), "vars": result.__dict__}
            )
            if results and results.strip():
                task.updateResources(results)

    def getResultKeys(self, task, results):
        return task.inputs.get("facts", [])

    def run(self, task):
        try:
            # build host inventory from resource
            inventory = self.getInventory(task)
            playbook = self.getPlaybook(task)

            # build vars from inputs
            extraVars = self.getVars(task)
            results = runPlaybooks(
                [playbook], inventory, extraVars, self.getPlaybookArgs(task)
            )

            if results.exit_code or len(results.resultsByStatus.failed):
                status = Status.error
            else:
                # unreachable, failed, skipped
                # XXX degraded vs. error if required?
                status = Status.ok

            logger.debug(
                "runplaybook status %s changed %s results %s ",
                status,
                results.changed,
                results.results[0]._check_key("result"),
            )

            # XXX if more then one task??
            result = (
                results.results
                and ansibleResults(
                    results.results[0], self.getResultKeys(task, results.results[0])
                )
                or None
            )
            if result and status == Status.ok or status == Status.degraded:
                # this can update resources so don't do it on error
                self._processResult(task, result)

            if results.changed > 0:
                if any(r.is_failed() and r.is_changed() for r in results.results):
                    modified = Status.error
                else:
                    modified = True
            else:
                modified = False
            yield task.done(
                status == Status.ok and not task.errors, modified, result=result
            )
        finally:
            self._cleanup()


# see https://github.com/ansible/ansible/blob/d72587084b4c43746cdb13abb262acf920079865/examples/scripts/uptime.py
# and https://github.com/ansible/ansible/blob/devel/lib/ansible/plugins/callback/log_plays.py
_ResultsByStatus = collections.namedtuple(
    "_ResultsByStatus", "ok failed skipped unreachable"
)


class ResultCallback(CallbackModule):
    # NOTE: callbacks will run in seperate process
    # see ansible.executor.task_result.TaskResult and ansible.playbook.task.Task

    def __init__(self):
        super(ResultCallback, self).__init__()
        self.results = []
        # named tuple of OrderedDict<task_name:list<result>>
        self.resultsByStatus = _ResultsByStatus(
            *[collections.OrderedDict() for x in range(4)]
        )
        self._load_name = "result"
        self.changed = 0

    def getInfo(self, result):
        host = result._host
        taskname = result.task_name
        fields = result._task_fields.keys()
        keys = result._result.keys()
        return "%s: %s(%s) => %s" % (host, taskname, fields, keys)

    def _addResult(self, status, result):
        self.results.append(result)
        if result._result.get("changed", False):
            self.changed += 1
        # XXX should save by host too
        getattr(self.resultsByStatus, status).setdefault(result.task_name, []).append(
            result
        )

    def v2_runner_on_ok(self, result):
        self._addResult("ok", result)
        # print("ok", self.getInfo(result))
        super(ResultCallback, self).v2_runner_on_ok(result)

    def v2_runner_on_skipped(self, result):
        self._addResult("skipped", result)
        # print("skipped", self.getInfo(result))
        super(ResultCallback, self).v2_runner_on_skipped(result)

    def v2_runner_on_failed(self, result, **kwargs):
        self._addResult("failed", result)
        # print("failed", self.getInfo(result))
        super(ResultCallback, self).v2_runner_on_failed(result, **kwargs)

    def v2_runner_on_unreachable(self, result):
        self._addResult("unreachable", result)
        # print("unreachable", self.getInfo(result))
        super(ResultCallback, self).v2_runner_on_unreachable(result)


def runPlaybooks(playbooks, _inventory, params=None, args=None):
    # unfurl.util should have initialized ansibleDummyCli and ansibleDisplay already
    inventoryArgs = ["-i", _inventory] if _inventory else []
    args = ["ansible-playbook"] + inventoryArgs + (args or []) + playbooks
    logger.info("running " + " ".join(args))
    cli = PlaybookCLI(args)
    cli.parse()

    # CallbackBase imports __main__.cli (which is set to ansibleDummyCli)
    # as assigns its options to self._options
    ansibleDummyCli.options.__dict__.update(cli.options.__dict__)

    # replace C.DEFAULT_STDOUT_CALLBACK with our own so we have control over logging
    # config/base.yml sets C.DEFAULT_STDOUT_CALLBACK == 'default' (ansible/plugins/callback/default.py)
    # (cli/console.py and cli/adhoc.py sets it to 'minimal' but PlaybookCLI.run() in cli/playbook.py uses the default)
    # see also https://github.com/projectatomic/atomic-host-tests/blob/master/callback_plugins/default.py
    resultsCB = ResultCallback()
    resultsCB.set_options()
    C.DEFAULT_STDOUT_CALLBACK = resultsCB

    _play_prereqs = cli._play_prereqs

    def hook_play_prereqs(options):
        loader, inventory, variable_manager = _play_prereqs(options)
        if params:
            variable_manager._extra_vars.update(params)
        resultsCB.inventoryManager = inventory
        resultsCB.variableManager = variable_manager
        # XXX inventory.localhost is None right now
        # inventory.localhost.set_variable("ansible_python_interpreter", sys.executable)
        return loader, inventory, variable_manager

    cli._play_prereqs = hook_play_prereqs

    oldVerbosity = ansibleDisplay.verbosity
    if logging.getLogger("ansible").getEffectiveLevel() <= 10:  # debug
        ansibleDisplay.verbosity = 2
    try:
        if cli.options.verbosity > ansibleDisplay.verbosity:
            ansibleDisplay.verbosity = cli.options.verbosity
        resultsCB.exit_code = cli.run()
    finally:
        ansibleDisplay.verbosity = oldVerbosity
    return resultsCB
