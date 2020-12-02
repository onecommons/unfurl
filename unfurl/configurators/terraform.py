# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
from ..util import saveToFile, UnfurlTaskError
from .shell import ShellConfigurator, which
from ..support import getdir, abspath, Status, writeFile
import json
import os
import os.path
import six
import re


def _getEnv(env, verbose, dataDir):
    env["TF_IN_AUTOMATION"] = "1"
    env["TF_INPUT"] = "0"
    # terraform currently only supports TF_LOG=TRACE
    # env["TF_LOG"] = "ERROR WARN INFO DEBUG TRACE".split()[verbose + 1]
    if verbose > 0:
        env["TF_LOG"] = "TRACE"

    # note: modules with relative paths get confused .terraform isn't child of the config dir
    # contains modules/modules.json and plugins/plugins.json:
    env["TF_DATA_DIR"] = dataDir
    return env


def markBlock(schema, items, task, sensitiveNames):
    blockTypes = schema.get("block_types", {})
    attributes = schema.get("attributes", {})
    for obj in items:
        for name, value in obj.items():
            attributeSchema = attributes.get(name)
            if attributeSchema:
                if attributeSchema.get("sensitive") or name in sensitiveNames:
                    #   mark sensistive
                    obj[name] = task.sensitive(value)
            else:
                if not value:
                    continue
                blockSchema = blockTypes.get(name)
                if blockSchema:
                    # "single", "map", "list", "set"
                    objectType = blockSchema["nesting_mode"]
                    if objectType == "single":
                        markBlock(blockSchema["block"], [value], task, sensitiveNames)
                    elif objectType == "map":
                        markBlock(
                            blockSchema["block"], value.values(), task, sensitiveNames
                        )
                    else:
                        markBlock(blockSchema["block"], value, task, sensitiveNames)


def markSensitive(schemas, state, task, sensitiveNames=()):
    for name, attrs in state["outputs"].items():
        value = attrs["value"]
        if attrs.get("sensitive") or name in sensitiveNames:
            state["outputs"][name]["value"] = task.sensitive(value)

    for resource in state["resources"]:
        provider = resource["provider"]
        type = resource["type"]
        providerSchema = schemas.get(provider) or schemas.get(
            provider.lstrip("provider.")
        )
        if providerSchema:
            schema = providerSchema["resource_schemas"].get(type)
            if schema:
                markBlock(schema["block"], resource["instances"], task, sensitiveNames)
            else:
                task.logger.warning(
                    "resource type '%s' not found in terraform schema", type
                )
        else:
            task.logger.warning("provider '%s' not found in terraform schema", provider)
    return state


class TerraformConfigurator(ShellConfigurator):
    _defaultCmd = "terraform"

    sensitiveNames = ["access_token", "key_material", "password"]

    def canDryRun(self, task):
        return True

    def _initTerraform(self, task, terraform, cwd, env):
        echo = task.verbose > -1
        timeout = task.configSpec.timeout
        cmd = terraform + ["init"]
        result = self.runProcess(cmd, timeout=timeout, env=env, cwd=cwd, echo=echo)
        if self._getStatusFromResult(task, result) != Status.ok:
            return None

        cmd = terraform + "providers schema -json".split(" ")
        result = self.runProcess(cmd, timeout=timeout, env=env, cwd=cwd, echo=False)
        if self._getStatusFromResult(task, result) != Status.ok:
            return None

        try:
            providerSchema = json.loads(result.stdout.strip())["provider_schemas"]
            # XXX add to ensemble "lock" section
            # os.path.join(env['TF_DATA_DIR'], "modules", "modules.json")
            # os.path.join(env['TF_DATA_DIR'], "plugins", "plugins.json")
            return providerSchema
        except:
            task.logger.debug("failed to load provider schema", exc_info=True)
            return None

    def _prepareWorkspace(self, cwd, task):
        """
        In terraform directory:
            Write out tf.json if necessary.
        """
        # generated tf.json get written to as main.unfurl.tmp.tf.json
        # (add *.tmp.* to git ignore)
        main = task.inputs.getCopy("main")
        if main:
            if isinstance(main, six.string_types):
                # assume its HCL
                path = os.path.join(cwd, "main.unfurl.tmp.tf")
            else:
                path = os.path.join(cwd, "main.unfurl.tmp.tf.json")
            ctx = task.inputs.context
            return writeFile(ctx, main, path)
        return None

    def _prepareVars(self, task):
        # XXX .tfvars can be sensitive
        # we need to output the plan and convert it to json to see which variables are marked sensitive
        tfvars = task.inputs.getCopy("tfvars")
        if tfvars:
            if isinstance(tfvars, six.string_types):
                # assume the contents of a tfvars file
                path = "vars.tfvars"
            else:
                path = "vars.tfvars.json"
            ctx = task.inputs.context
            return writeFile(ctx, tfvars, path, "local")
        return None

    def _prepareState(self, task, ctx):
        # the terraform state file is associate with the current instance
        # read the (possible encrypted) version from the repository
        # and write out it as plaintext json into the local directory
        yamlPath = abspath(ctx, "terraform.tfstate.yaml", "home", False)
        jsonPath = abspath(ctx, "terraform.tfstate.json", "local", False)
        if not os.path.exists(jsonPath) and os.path.exists(yamlPath):
            # if exists in home, load and write out state file as json
            with open(yamlPath, "r") as f:
                state = task._manifest.yaml.load(f.read())
            writeFile(ctx, state, jsonPath)
        return jsonPath

    def run(self, task):
        ctx = task.inputs.context
        echo = task.verbose > -1
        # options:
        _, terraform = self._cmd(
            task.inputs.get("command", self._defaultCmd), task.inputs.get("keeplines")
        )
        cwd = os.path.abspath(task.inputs.get("dir") or getdir(ctx, "spec.home"))
        dataDir = os.getenv("TF_DATA_DIR", os.path.join(cwd, ".terraform"))

        # write out any needed files to cwd, eg. main.tf.json
        self._prepareWorkspace(cwd, task)
        # write vars to file in local if necessary
        varfilePath = self._prepareVars(task)
        # write the state file to local if necessary
        statePath = self._prepareState(task, ctx)

        env = _getEnv(task.getEnvironment(False), task.verbose, dataDir)

        ### Load the providers schemas and run terraform init if necessary
        providerSchemaPath = os.path.join(dataDir, "providers-schema.json")
        if os.path.exists(providerSchemaPath):
            with open(providerSchemaPath) as psf:
                providerSchema = json.load(psf)
        else:  # first time
            providerSchema = self._initTerraform(task, terraform, cwd, env)
            if providerSchema:
                saveToFile(providerSchemaPath, providerSchema)
            else:
                raise UnfurlTaskError(task, "terraform init failed")

        # build the command line and run it
        if task.dryRun:
            action = ["plan", "-state=" + statePath, "-detailed-exitcode"]
            if task.configSpec.operation == "delete":
                action.append("-destroy")
        elif task.configSpec.operation == "check":
            action = ["refresh", "-state=" + statePath]
        elif task.configSpec.operation == "delete":
            action = ["destroy", "-auto-approve", "-state=" + statePath]
        elif task.configSpec.workflow == "deploy":
            action = ["apply", "-auto-approve", "-state=" + statePath]
        else:
            raise UnfurlTaskError(
                task, "unexpected operation: " + task.configSpec.operation
            )
        cmd = terraform + action
        if varfilePath:
            cmd.append("-var-file=" + varfilePath)

        result = self.runProcess(
            cmd, timeout=task.configSpec.timeout, env=env, cwd=cwd, echo=echo
        )
        if result.returncode and re.search(r"terraform\s+init", result.stderr):
            # modules or plugins out of date, re-run terraform init
            providerSchema = self._initTerraform(task, terraform, cwd, env)
            if providerSchema:
                saveToFile(providerSchemaPath, providerSchema)
                # try again
                result = self.runProcess(
                    cmd, timeout=task.configSpec.timeout, env=env, cwd=cwd, echo=echo
                )
            else:
                raise UnfurlTaskError(task, "terrform init failed")

        # process the result
        if result.returncode == 2:
            status = Status.ok
        else:
            status = self._getStatusFromResult(task, result)

        if status == Status.ok and not task.dryRun:
            # read state file
            with open(statePath) as sf:
                state = json.load(sf)
            state = markSensitive(providerSchema, state, task, self.sensitiveNames)
            # save state file in home as yaml, encrypting sensitive values
            writeFile(ctx, state, "terraform.tfstate.yaml", "home")
            outputs = {name: attrs["value"] for name, attrs in state["outputs"].items()}
            state.update(result.__dict__)
            self.processResultTemplate(task, state)
        else:
            outputs = None

        # XXX how to tell if modified?
        # requires calling plan with -detailed-exitcode to set modified: 0 no change, 1 error, 2 modified (plan only)
        yield task.done(status == Status.ok, result=result.__dict__, outputs=outputs)


# XXX implement discover:
# terraform import -allow-missing-config {type.name}
# convert resource schemas to TOSCA types?
# see https://www.terraform.io/docs/extend/schemas/schema-types.html#types
# types: string int float64 list set map
# behaviors: Default optional required computed (=> attribute) ForceNew: instance-key, sensitive
