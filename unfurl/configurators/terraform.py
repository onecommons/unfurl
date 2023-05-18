# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
from typing import TYPE_CHECKING

from ..runtime import EntityInstance
from ..util import save_to_file, UnfurlTaskError, wrap_var, which
from .shell import ShellConfigurator, clean_output, make_regex_filter
from ..support import Status
from ..result import Result
from ..projectpaths import get_path, FilePath, Folders
import json
import os
import os.path
import re


def _get_env(env, verbose, dataDir):
    env["TF_IN_AUTOMATION"] = "1"
    env["TF_INPUT"] = "0"
    # see https://www.terraform.io/plugin/log/managing
    # env["TF_LOG"] = "ERROR WARN INFO DEBUG TRACE".split()[verbose + 1]
    if verbose >= 0:
        # providers can be very verbose, don't set them to debug
        env["TF_LOG_CORE"] = "DEBUG"

    # note: modules with relative paths get confused .terraform isn't child of the config dir
    # contains modules/modules.json and plugins/plugins.json:
    env["TF_DATA_DIR"] = dataDir
    return env


def get_echo_args(verbosity):
    if verbosity == -1:  # quiet mode
        return dict(echo=False)  # no stdout or stderr
    else:
        logregex = re.compile(r"\[(TRACE|DEBUG|INFO|WARN|ERROR)\]")
        if verbosity == 0:  # default
            levels = "INFO|WARN|ERROR"
        else:  # verbose == 1
            levels = "TRACE|DEBUG|INFO|WARN|ERROR"
        stderr_filter = make_regex_filter(logregex, levels.split("|"))
    return dict(echo=True, stderr_filter=stderr_filter)


def mark_block(schema, items, task, sensitive_names):
    blockTypes = schema.get("block_types", {})
    attributes = schema.get("attributes", {})
    for obj in items:
        for name, value in obj.items():
            attributeSchema = attributes.get(name)
            if attributeSchema:
                if attributeSchema.get("sensitive") or name in sensitive_names:
                    #   mark sensitive
                    obj[name] = task.sensitive(value)
            else:
                if not value:
                    continue
                blockSchema = blockTypes.get(name)
                if blockSchema:
                    # "single", "map", "list", "set"
                    objectType = blockSchema["nesting_mode"]
                    if objectType == "single":
                        mark_block(blockSchema["block"], [value], task, sensitive_names)
                    elif objectType == "map":
                        mark_block(
                            blockSchema["block"], value.values(), task, sensitive_names
                        )
                    else:
                        mark_block(blockSchema["block"], value, task, sensitive_names)


def mark_sensitive(schemas, state, task, sensitive_names=()):
    for name, attrs in state["outputs"].items():
        value = attrs["value"]
        if attrs.get("sensitive") or name in sensitive_names:
            state["outputs"][name]["value"] = task.sensitive(value)

    # XXX use sensitive_attributes to find attributes to mark sensitive
    for resource in state["resources"]:
        provider = resource["provider"]
        type = resource["type"]
        providerSchema = schemas.get(provider) or schemas.get(
            provider.lstrip("provider.")
        )
        if providerSchema:
            schema = providerSchema["resource_schemas"].get(type)
            if schema:
                mark_block(
                    schema["block"], resource["instances"], task, sensitive_names
                )
            else:
                task.logger.warning(
                    "resource type '%s' not found in terraform schema", type
                )
        else:
            # XXX providers schema is probably out of date, retrieve schema again?
            task.logger.info("provider '%s' not found in terraform schema", provider)
    return state


def _get_tfvars_from_properties(instance):
    return {
        p.name: instance.attributes[p.name]
        for p in instance.template.propertyDefs.values()
        if p.schema.get("metadata", {}).get("tfvar")
    }


_main_tf_template = """\
module "main" {
 source = "%s"

%s
}
%s
"""


def generate_main(relpath, tfvars, outputs):
    # XXX until we can check if an output is sensitive
    # we always need to set them all to sensitive to avoid a terraform error
    # when the referenced output is sensitive
    sensitive = True
    # if tfvars are hcl:
    if isinstance(tfvars, str):
        output = ""
        for name in outputs:
            if sensitive:
                sensitive_str = "sensitive=true\n"
            else:
                sensitive_str = ""
            output += f'output "{name}" {{\n value = module.main.{name}\n {sensitive_str} }}\n'
        return "main.tmp.tf", _main_tf_template % (relpath, tfvars, output)
    else:
        # place tfvars in the module block:
        module = tfvars.copy() if tfvars else {}
        module["source"] = relpath
        root = dict(module=dict(main=module))
        if outputs:
            root["output"] = {}
            for name in outputs:
                root["output"][name] = dict(
                    value=f"${{module.main.{name}}}", sensitive=sensitive
                )
        return "main.tmp.tf.json", root


def _needs_init(msg):
    return re.search(r"terraform\W+init", msg)


class TerraformConfigurator(ShellConfigurator):
    _default_cmd = "terraform"
    attribute_output_metadata_key = "tfoutput"

    # provider schemas don't always mark keys as sensitive that they should, so just in case:
    sensitive_names = [
        "access_token",
        "key_material",
        "password",
        "private_key",
        "server_ca_cert",
    ]

    @classmethod
    def set_config_spec_args(klass, kw: dict, target: EntityInstance):
        if not which("terraform"):
            artifact = target.template.find_or_create_artifact(
                "terraform", predefined=True
            )
            if artifact:
                kw["dependencies"].append(artifact)
        return kw

    def can_dry_run(self, task):
        return True

    def _init_terraform(self, task, terraform, folder, env):
        # only retrieve the schema when we need to worry about sensitive data
        # in the terraform state file.
        # (though we still try to mark data as sensitive even without it)
        get_provider_schema = self._get_workfolder_name(task) not in [
            "remote",
            "secrets",
        ]
        # folder is "tasks" WorkFolder
        cwd = folder.cwd
        lock_file = task.set_work_folder(Folders.artifacts).permanent_path(
            ".terraform.lock.hcl", False
        )
        if os.path.exists(lock_file):
            folder.copy_from(lock_file)

        echo = task.verbose > -1
        timeout = task.configSpec.timeout
        cmd = terraform + ["init"]
        result = self.run_process(cmd, timeout=timeout, env=env, cwd=cwd, echo=echo)
        if not self._handle_result(task, result, cwd):
            return None

        if os.path.exists(folder.get_current_path(".terraform.lock.hcl", False)):
            folder.copy_to(lock_file)

        if not get_provider_schema:
            return {}

        cmd = terraform + "providers schema -json".split(" ")
        result = self.run_process(cmd, timeout=timeout, env=env, cwd=cwd, echo=False)
        if not self._handle_result(task, result, cwd):
            task.logger.warning(
                "terraform providers schema failed: %s %s",
                result.returncode,
                result.stderr,
            )
            return None

        try:
            providerSchema = json.loads(result.stdout.strip())
            # XXX add to ensemble "lock" section
            # os.path.join(env['TF_DATA_DIR'], "modules", "modules.json")
            # os.path.join(env['TF_DATA_DIR'], "plugins", "plugins.json")
            # missing if there are no providers:
            return providerSchema.get("provider_schemas", {})
        except:
            task.logger.debug("failed to load provider schema", exc_info=True)
            return None

    def _get_outputs(self, task):
        outputs = [
            p.name
            for p in task.target.template.attributeDefs.values()
            if p.schema.get("metadata", {}).get(self.attribute_output_metadata_key)
        ]
        if task.configSpec.outputs:
            if isinstance(task.configSpec.outputs, (str, int)):
                raise UnfurlTaskError(
                    task,
                    f'Invalid Terraform outputs specified "{task.configSpec.outputs}"',
                )
            outputs.extend(task.configSpec.outputs)
        return outputs

    def _get_tfvars(self, task):
        tfvars = task.inputs.get_copy("tfvars")
        if not isinstance(tfvars, str):
            tfprops = _get_tfvars_from_properties(task.target)
            if isinstance(tfvars, dict):
                tfvars.update(tfprops)
            else:
                return tfprops
        return tfvars

    def _prepare_workspace(self, task, cwd):
        """
        In terraform directory:
            Write out tf.json if necessary.
        """
        # generated tf.json get written to as main.unfurl.tmp.tf.json
        write_vars = True
        contents = None
        main = task.inputs.get_copy("main")
        if not main:
            main = get_path(task.inputs.context, task.target.name, "src")
            if not os.path.exists(main):
                raise UnfurlTaskError(
                    task,
                    f'Input parameter "main" not specifed and default terraform module directory does not exist at "{main}"',
                )
        if task._errors:
            main = None  # assume render failed
        if isinstance(main, str):
            if "\n" in main:
                # assume its HCL and not a path
                contents = main
                path = "main.unfurl.tmp.tf"
            else:
                if not os.path.isabs(main):
                    main = get_path(task.inputs.context, main, "src")
                if os.path.exists(main):
                    # it's a directory -- if difference from cwd, treat it as a module to call
                    relpath = cwd.relpath_to_current(main)
                    if relpath != ".":
                        write_vars = False
                        outputs = self._get_outputs(task)
                        tfvars = self._get_tfvars(task)
                        path, contents = generate_main(relpath, tfvars, outputs)

                    # set this as FilePath so we can monitor changes to it
                    result = task.inputs._attributes["main"]
                    if not isinstance(result, Result) or not result.external:
                        task.inputs["main"] = FilePath(main)
                else:
                    raise UnfurlTaskError(
                        task, f'Terraform module directory "{main}" does not exist'
                    )
        else:  # assume it json
            contents = main
            path = "main.unfurl.tmp.tf.json"

        if write_vars:
            varpath = self._prepare_vars(task, cwd)
        else:
            varpath = None
        if contents:
            mainpath = cwd.write_file(contents, path)
        else:
            mainpath = None
        return mainpath, varpath

    def _prepare_vars(self, task, cwd):
        # XXX .tfvars can be sensitive
        # we need to output the plan and convert it to json to see which variables are marked sensitive
        tfvars = self._get_tfvars(task)
        if task._errors:
            return None  # assume render failed
        if tfvars:
            if isinstance(tfvars, str):
                # assume the contents of a tfvars file
                path = "vars.tmp.tfvars"
            else:
                path = "vars.tmp.tfvars.json"
            return cwd.write_file(tfvars, path)
        return None

    def _get_workfolder_name(self, task):
        return (
            task.inputs.get("stateLocation") or Folders.secrets
        )  # XXX global option for secrets

    def _prepare_state(self, task, cwd):
        # the terraform state file is associate with the current instance
        # read the (possible encrypted) version from the repository
        # and write out it as plaintext json into the local directory
        folderName = self._get_workfolder_name(task)
        if folderName == "remote":  # don't use local state file
            return ""
        yamlPath = task.set_work_folder(folderName).permanent_path(
            "terraform.tfstate.yaml", False
        )
        if os.path.exists(yamlPath):
            task.logger.debug("Found existing terraform.tfstate file at %s", yamlPath)
            # if exists in home, load and write out state file as json
            with open(yamlPath, "r") as f:
                state = task._manifest.yaml.load(f.read())
            cwd.write_file(state, "terraform.tfstate")
        else:
            task.logger.debug("Couldn't find terraform.tfstate file at %s", yamlPath)
        return "terraform.tfstate"

    def _get_plan_path(self, task, cwd):
        # the terraform state file is associate with the current instance
        # read the (possible encrypted) version from the repository
        # and write out it as plaintext json into the local directory
        jobId = task.get_job_id(task.changeId)
        return cwd.get_current_path(jobId + ".plan")

    def render(self, task):
        workdir = task.inputs.get("workdir") or Folders.tasks
        cwd = task.set_work_folder(workdir, preserve=True)

        _, terraformcmd = self._cmd(
            task.inputs.get("command", self._default_cmd), task.inputs.get("keeplines")
        )

        # write out any needed files to cwd, eg. main.tf.json
        mainpath, varfilePath = self._prepare_workspace(task, cwd)
        # write the state file to local if necessary
        statePath = self._prepare_state(task, cwd)

        planPath = self._get_plan_path(task, cwd)
        # build the command line and run it
        if task.dry_run or task.configSpec.operation == "check":
            action = [
                "plan",
                "-detailed-exitcode",
                "-refresh=true",
                "-out",
                planPath,
            ]
            if statePath:
                action.append("-state=" + statePath)
            if task.configSpec.operation == "delete":
                action.append("-destroy")
        elif (
            task.configSpec.operation == "delete"
            or task.configSpec.workflow == "undeploy"
        ):
            action = ["destroy", "-auto-approve"]
            if statePath:
                action.append("-state=" + statePath)
        elif task.configSpec.workflow == "deploy":
            action = ["apply", "-auto-approve"]
            if statePath:
                action.append("-state=" + statePath)
            if os.path.isfile(planPath) and os.path.isfile(statePath):
                action.append(
                    planPath
                )  # use plan created by previous operation in this job
        else:
            raise UnfurlTaskError(
                task, "unexpected operation: " + task.configSpec.operation
            )
        cmd = terraformcmd + action
        if varfilePath:
            cmd.append("-var-file=" + varfilePath)

        return [cmd, terraformcmd, statePath]

    def run(self, task):
        cwd = task.get_work_folder(Folders.tasks)
        cmd, terraform, statePath = task.rendered
        current_path = cwd.cwd
        dataDir = os.getenv("TF_DATA_DIR", os.path.join(current_path, ".terraform"))
        env = _get_env(task.environ, task.verbose, dataDir)

        ### Load the providers schemas and run terraform init if necessary
        providerSchemaPath = os.path.join(dataDir, "providers-schema.json")
        if os.path.exists(providerSchemaPath):
            with open(providerSchemaPath) as psf:
                providerSchema = json.load(psf)
        elif not os.path.exists(os.path.join(dataDir, "providers")):  # first time
            providerSchema = self._init_terraform(task, terraform, cwd, env)
            if providerSchema is not None:
                save_to_file(providerSchemaPath, providerSchema)
            else:
                raise UnfurlTaskError(task, f"terraform init failed in {current_path}")
        else:
            providerSchema = {}

        echo_args = get_echo_args(task.verbose)
        result = self.run_process(
            cmd, timeout=task.configSpec.timeout, env=env, cwd=cwd.cwd, **echo_args
        )
        if result.returncode and _needs_init(clean_output(result.stderr)):
            # modules or plugins out of date, re-run terraform init
            providerSchema = self._init_terraform(task, terraform, cwd, env)
            if providerSchema is not None:
                save_to_file(providerSchemaPath, providerSchema)
                # try again
                result = self.run_process(
                    cmd,
                    timeout=task.configSpec.timeout,
                    env=env,
                    cwd=cwd.cwd,
                    **echo_args,
                )
            else:
                raise UnfurlTaskError(
                    task,
                    f"terrform init failed in {cwd.cwd}; TF_DATA_DIR={dataDir}",
                )

        # process the result
        status = None
        success = self._handle_result(task, result, cwd.cwd, (0, 2))
        # plan -detailed-exitcode: 2 - Succeeded, but there is a diff
        needs_changes = False
        if result.returncode == 2:
            success = True  # command succeeded despite non-zero return code
            # we check for "Plan:" in the output because sensitive outputs will always be marked as changed
            # so to know if changes are really needed we also look for a message like: Plan: 1 to add, 0 to change, 0 to destroy.
            needs_changes = "Plan:" in result.stdout
            if task.configSpec.operation != "check":
                # changes were applied so set to OK
                status = Status.ok

        if success and task.configSpec.operation == "check":
            if needs_changes:  # treat as missing
                status = Status.absent
            elif task.target.status in [Status.pending, Status.unknown]:
                # no changes needed so set to known state
                status = Status.ok

        modified = (
            "Modifying..." in result.stdout
            or "Creating..." in result.stdout
            or "Destroying..." in result.stdout
        )

        if not task.dry_run and task.configSpec.operation != "check":
            outputs = {}
            current_path = cwd.cwd
            if statePath and os.path.isfile(os.path.join(current_path, statePath)):
                # read state file
                statePath = os.path.join(current_path, statePath)
                with open(statePath) as sf:
                    state = json.load(sf)
                state = mark_sensitive(
                    providerSchema, state, task, self.sensitive_names
                )
                # save state file in home as yaml, encrypting sensitive values
                folderName = self._get_workfolder_name(task)
                # set always_apply because we want to commit the terraform state file
                # even if the terraform command failed (as it might have updated some resources)
                task.set_work_folder(folderName, always_apply=True).write_file(
                    state, "terraform.tfstate.yaml"
                )
                outputs = {
                    name: wrap_var(attrs["value"])
                    for name, attrs in state["outputs"].items()
                }
                state.update(result.__dict__)
                state["outputs"] = outputs  # replace outputs
                state["success"] = success
                state["modified"] = modified
                errors, new_status = self.process_result_template(task, state)
                if success:
                    success = not errors
                if new_status is not None:
                    status = new_status
            else:
                state = {}
        else:
            outputs = None

        yield self.done(
            task,
            success=success,
            modified=modified,
            status=status,
            result=result.__dict__,
            outputs=outputs,
        )


# XXX implement discover:
# terraform import -allow-missing-config {type.name}
# convert resource schemas to TOSCA types?
# see https://www.terraform.io/docs/extend/schemas/schema-types.html#types
# types: string int float64 list set map
# behaviors: Default optional required computed (=> attribute) ForceNew: instance-key, sensitive
