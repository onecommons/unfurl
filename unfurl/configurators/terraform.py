# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
from typing import (
    TYPE_CHECKING,
    Iterable,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
    Union,
    Dict,
    Any,
    cast,
)
from typing_extensions import Literal

from ..planrequests import ConfigurationSpecKeywords
from ..configurator import TaskView
from ..spec import EntitySpec
from ..logs import getLogger
from ..util import UnfurlTaskError, which
from .shell import ShellConfigurator, ShellInputs, clean_output, make_regex_filter
from ..support import Status
from ..result import Result, wrap_var
from ..projectpaths import WorkFolder, get_path, _abspath, Folders
import json
import os
import os.path
import re
import subprocess
import tempfile
import tosca

# not task.logger, which demotes warnings while rendering
logger = getLogger("unfurl")


class TerraformInputs(ShellInputs):
    main: Union[None, str, Dict[str, Any]] = None
    tfvars: Union[None, str, Dict[str, Any]] = None
    stateLocation: Literal["secrets", "artifacts", "remote"] = "secrets"
    workdir: Union[None, str] = None
    dryrun_mode: Literal["plan", "real"] = "plan"
    dryrun_output: Union[None, str, Dict[str, Any]] = None


Version = Tuple[int, int, int]
TfCli = Tuple[Literal["tofu", "terraform"], Version]

MIN_TOFU_VERSION: Version = (1, 6, 0)
RECOMMENDED_TOFU_VERSION: Version = (1, 12, 0)
# carries hashicorp/* lock entries over from a Terraform lock file
TOFU_LOCK_SHIM_VERSION: Version = (1, 10, 0)
TOFU_XDG_CONFIG_VERSION: Version = (1, 7, 0)
MIN_TERRAFORM_VERSION: Version = (1, 1, 0)

# by resolved path, None if its version couldn't be parsed
_tf_cli_versions: Dict[str, Optional[TfCli]] = {}
_tf_cli_warned: Set[str] = set()
# (job id, instance key)
_migration_warned: Set[Tuple[str, str]] = set()


def _version_str(version: Version) -> str:
    return ".".join(str(n) for n in version)


def _tf_cli_version(path: str, env: Mapping[str, str]) -> Optional[TfCli]:
    """Return the kind and version of the executable at ``path``, running ``<path> version`` once per path."""
    if path not in _tf_cli_versions:
        try:
            output = subprocess.run(
                [path, "version"],
                capture_output=True,
                text=True,
                timeout=60,
                # terraform otherwise asks checkpoint-api.hashicorp.com for the latest version
                env={**env, "CHECKPOINT_DISABLE": "1"},
            ).stdout
        except (OSError, subprocess.SubprocessError):
            output = ""
        match = re.match(r"\s*(OpenTofu|Terraform) v(\d+)\.(\d+)\.(\d+)", output)
        _tf_cli_versions[path] = (
            (
                "tofu" if match[1] == "OpenTofu" else "terraform",
                (int(match[2]), int(match[3]), int(match[4])),
            )
            if match
            else None
        )
    return _tf_cli_versions[path]


def _warn_once(path: str, msg: str, *args: object) -> None:
    if path not in _tf_cli_warned:
        _tf_cli_warned.add(path)
        logger.warning(msg, *args)


def _check_tf_cli(task: TaskView, command: str) -> Optional[TfCli]:
    """Return the kind and version of ``command``, raising :class:`UnfurlTaskError` if it's too old."""
    found = which(command, path=task.environ.get("PATH"))
    if not found:
        return None  # running it reports that it's missing
    path = os.path.realpath(found)
    cli = _tf_cli_version(path, task.environ)
    if cli is None:
        _warn_once(
            path,
            'Could not determine the version of "%s": Unfurl supports only OpenTofu %s or later',
            found,
            _version_str(MIN_TOFU_VERSION),
        )
        return None
    kind, version = cli
    if kind == "terraform":
        if version < MIN_TERRAFORM_VERSION:
            raise UnfurlTaskError(
                task,
                f"Terraform {_version_str(version)} at {found} is too old: the command input requires Terraform {_version_str(MIN_TERRAFORM_VERSION)} or later",
            )
        _warn_once(
            path,
            "Running HashiCorp Terraform %s (%s) via the `command` input. Unfurl supports only OpenTofu: "
            "you're responsible for this deployment's state compatibility. Running Terraform on state OpenTofu has written "
            "makes Terraform try to install the registry.opentofu.org providers it references (see the Unfurl upgrade notes).",
            _version_str(version),
            found,
        )
    elif version < MIN_TOFU_VERSION:
        raise UnfurlTaskError(
            task,
            f"OpenTofu {_version_str(version)} at {found} is too old: Unfurl requires OpenTofu {_version_str(MIN_TOFU_VERSION)} or later "
            f"({_version_str(RECOMMENDED_TOFU_VERSION)} or later recommended)",
        )
    elif version < RECOMMENDED_TOFU_VERSION:
        lock_clause = (
            ""
            if version >= TOFU_LOCK_SHIM_VERSION
            else " before 1.10: it doesn't carry locked hashicorp/* provider versions over from a Terraform lock file, "
            "so they re-resolve to the newest matching version on migration;"
        )
        _warn_once(
            path,
            "OpenTofu %s at %s is older than 1.12: it checks `required_version` in .tf files against its own version, "
            "so modules written for Terraform may be rejected;%s and it no longer receives upstream security fixes. "
            "Upgrade to 1.12 or later.",
            _version_str(version),
            found,
            lock_clause,
        )
    return cli


_TERRAFORM_REGISTRY = "registry.terraform.io/"


def _needs_migration(state: Optional[Mapping[str, Any]], lock_file: str) -> bool:
    """Return whether the state, or if it has no resources the lock file, has Terraform registry provider addresses."""
    resources = state.get("resources") if state else None
    if resources:
        return any(
            _TERRAFORM_REGISTRY in str(resource.get("provider", ""))
            for resource in resources
        )
    return any(
        address.startswith(_TERRAFORM_REGISTRY)
        for address in _locked_providers(lock_file)
    )


def _cli_config_files(env: Mapping[str, str], cli: Optional[TfCli]) -> List[str]:
    """Return the CLI config files ``cli`` reads, in order of precedence."""
    override = env.get("TF_CLI_CONFIG_FILE") or env.get("TERRAFORM_CONFIG")
    if override:
        return [override]  # the config directory isn't read either
    home = env.get("HOME") or os.path.expanduser("~")
    tofurc = os.path.join(home, ".tofurc")
    terraformrc = os.path.join(home, ".terraformrc")
    config_dir = os.path.join(home, ".terraform.d")
    xdg_config = None
    if cli and cli[0] == "terraform":
        main = terraformrc
    else:
        if cli and cli[1] >= TOFU_XDG_CONFIG_VERSION:
            xdg_config = env.get("XDG_CONFIG_HOME")
        if xdg_config and not os.path.exists(terraformrc) and not os.path.exists(tofurc):
            main = os.path.join(xdg_config, "opentofu", "tofurc")
        elif os.path.exists(terraformrc) and not os.path.exists(tofurc):
            main = terraformrc
        else:
            main = tofurc
        if xdg_config and not os.path.exists(config_dir):
            config_dir = os.path.join(xdg_config, "opentofu")
    try:
        names = sorted(os.listdir(config_dir))
    except OSError:
        names = []
    return [main] + [
        os.path.join(config_dir, name)
        for name in names
        if name.endswith(".tfrc") or name.endswith(".tfrc.json")
    ]


def _read_plugin_cache_dir(rcfile: str) -> Optional[str]:
    """Return the ``plugin_cache_dir`` set in the CLI config file ``rcfile``, if any."""
    try:
        with open(rcfile) as f:
            contents = f.read()
    except OSError:
        return None
    if rcfile.endswith(".json"):
        try:
            config = json.loads(contents)
        except ValueError:
            return None
        value = config.get("plugin_cache_dir") if isinstance(config, dict) else None
        return value if isinstance(value, str) else None
    m = re.search(r'^\s*plugin_cache_dir\s*=\s*"([^"]*)"', contents, re.MULTILINE)
    return m.group(1) if m else None


def _default_plugin_cache_dir(
    env: Mapping[str, str], cli: Optional[TfCli] = None
) -> Optional[str]:
    """Return the ``plugin_cache_dir`` that ``cli``'s CLI config sets, None if it's
    set to "", otherwise ``~/.terraform.d/plugin-cache``."""
    for rcfile in _cli_config_files(env, cli):
        cache_dir = _read_plugin_cache_dir(rcfile)
        if cache_dir:
            return os.path.expanduser(os.path.expandvars(cache_dir))
        if cache_dir == "":
            # opts out of unfurl's default
            return None
    return os.path.expanduser("~/.terraform.d/plugin-cache")


def _get_env(
    env: Dict[str, str], verbose: int, dataDir: str, cli: Optional[TfCli] = None
) -> Dict[str, str]:
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
    # Auto-populate TF_PLUGIN_CACHE_DIR so concurrent terraform tasks share
    # a provider cache (and unfurl knows to serialize init on it). Honors
    # an existing env var or CLI config file setting first.
    if not env.get("TF_PLUGIN_CACHE_DIR"):
        default = _default_plugin_cache_dir(env, cli)
        if default:
            env["TF_PLUGIN_CACHE_DIR"] = default
    # When TF_PLUGIN_CACHE_DIR is set, terraform >=1.4 refuses by default to
    # write a lock file with fewer hashes than it already contains (cache
    # installs produce fewer hash types than registry installs). Default to
    # "true" so init succeeds against the cache. Users who care about the
    # strict hash set can set this to "false" to opt back into the strict
    # behavior (and accept the init failures it brings).
    if env.get("TF_PLUGIN_CACHE_DIR"):
        env.setdefault("TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE", "true")
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


def _schema_cache_dir(schema_dir: str, terraform: List[str]) -> str:
    """Return the provider schema cache directory for the ``terraform`` command."""
    name = re.sub(r"[^\w.-]", "_", os.path.basename(terraform[0]))
    return os.path.join(schema_dir, f"providers-schema-{name}")


def _schema_cache_file(cache_dir: str, address: str, version: str) -> str:
    """Return the cache file for version ``version`` of the provider at ``address``."""
    name = re.sub(r"[^\w.@-]", "_", f"{address}@{version}")
    return os.path.join(cache_dir, f"{name}.json")


def _load_schema_cache(path: str) -> Optional[Dict[str, Any]]:
    """Return the provider schema cached at ``path``, or None if it can't be read."""
    try:
        with open(path) as f:
            schema = json.load(f)
    except (OSError, ValueError):
        return None
    return schema if isinstance(schema, dict) and schema else None


def _save_schema_cache(path: str, schema: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # concurrent tasks and processes share the cache, so never expose a partial file
    with tempfile.NamedTemporaryFile(
        "w", dir=os.path.dirname(path), suffix=".tmp", delete=False
    ) as f:
        json.dump(schema, f)
    os.replace(f.name, path)


def _locked_providers(lock_file: str) -> Dict[str, str]:
    """Map each provider address in a ``.terraform.lock.hcl`` file to its version."""
    try:
        with open(lock_file) as f:
            contents = f.read()
    except OSError:
        return {}
    providers = {}
    for block in re.finditer(
        r'^provider "([^"]+)" \{(.*?)^\}', contents, re.MULTILINE | re.DOTALL
    ):
        version = re.search(
            r'^\s*version\s*=\s*"([^"]+)"', block.group(2), re.MULTILINE
        )
        if version:
            providers[block.group(1)] = version.group(1)
    return providers


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


_provider_address_re = re.compile(r'provider\["([^"]+)"\]')
_legacy_provider_address_re = re.compile(r"provider\.([\w-]+)")

_public_registries = ("registry.terraform.io", "registry.opentofu.org")


def find_provider_schema_key(
    provider: str, schema_keys: Iterable[str]
) -> Optional[str]:
    """Return the key in ``schema_keys`` for the state's provider address ``provider``
    (e.g. ``module.main.provider["registry.terraform.io/hashicorp/aws"].west``)."""
    match = _provider_address_re.search(provider)
    if not match:
        match = _legacy_provider_address_re.search(provider)
    if not match:
        return None
    source = match.group(1)
    keys = set(schema_keys)
    if source in keys:
        return source
    # the cached schema may come from the other binary (terraform or tofu),
    # which keys providers by its own registry's hostname
    host, _, name = source.partition("/")
    if host in _public_registries:
        for other_host in _public_registries:
            if f"{other_host}/{name}" in keys:
                return f"{other_host}/{name}"
    return None


def mark_sensitive(schemas, state, task, sensitive_names=()):
    for name, attrs in state["outputs"].items():
        value = attrs["value"]
        if attrs.get("sensitive") or name in sensitive_names:
            state["outputs"][name]["value"] = task.sensitive(value)

    # without a vault password the state would be saved with the values redacted
    if not task._manifest.yaml.representer.vault.secrets:
        return state

    # XXX use sensitive_attributes to find attributes to mark sensitive
    for resource in state["resources"]:
        provider = resource["provider"]
        type = resource["type"]
        schema_key = find_provider_schema_key(provider, schemas)
        if schema_key:
            schema = schemas[schema_key]["resource_schemas"].get(type)
            if schema:
                instance_attributes = [
                    instance["attributes"]
                    for instance in resource["instances"]
                    if instance.get("attributes")
                ]
                mark_block(schema["block"], instance_attributes, task, sensitive_names)
            else:
                task.logger.warning(
                    "resource type '%s' not found in terraform schema", type
                )
        else:
            # XXX providers schema is probably out of date, retrieve schema again?
            task.logger.info("provider '%s' not found in terraform schema", provider)
    return state


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
    return re.search(r"(terraform|tofu)\W+init", msg)


class TerraformConfigurator(ShellConfigurator):
    _default_cmd = "tofu"
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
    def set_config_spec_args(
        cls, kw: ConfigurationSpecKeywords, template: EntitySpec
    ) -> ConfigurationSpecKeywords:
        if not (kw.get("inputs") or {}).get("command") and not which(cls._default_cmd):
            artifact = template.find_or_create_artifact("opentofu", predefined=True)
            if artifact:
                kw.setdefault("dependencies", []).append(artifact)
        # add_path_transform(kw, "main", template)
        return kw

    @classmethod
    def get_dry_run(cls, inputs, template) -> bool:
        # default: defer to mock implementation if present otherwise defer to runtime check (can_dry_run())
        return bool(inputs.get("dryrun_outputs"))

    def can_dry_run(self, task) -> bool:
        return True

    def _run_init(
        self,
        task: TaskView,
        terraform: List[str],
        folder: WorkFolder,
        env: Dict[str, str],
    ) -> bool:
        """Run ``terraform init`` in ``folder``. Unless it's a dry run, persists the
        resulting ``.terraform.lock.hcl`` to the task's artifact folder so subsequent
        runs reuse the same provider selections. Returns True on success."""
        cwd = folder.cwd
        lock_file = task.set_work_folder(Folders.artifacts).permanent_path(
            ".terraform.lock.hcl", False
        )
        if os.path.exists(lock_file):
            folder.copy_from(lock_file)

        echo = task.verbose > -1
        cmd = terraform + ["init"]
        result = self.run_process(
            cmd, timeout=task.configSpec.timeout, env=env, cwd=cwd, echo=echo
        )
        if not self._handle_result(task, result, cwd, env=env):
            return False

        if not task.dry_run and os.path.exists(
            folder.get_current_path(".terraform.lock.hcl", False)
        ):
            folder.copy_to(lock_file)
        return True

    def _fetch_provider_schemas(
        self,
        task: TaskView,
        terraform: List[str],
        folder: WorkFolder,
        env: Dict[str, str],
    ) -> Optional[Dict[str, Any]]:
        cwd = folder.cwd
        timeout = task.configSpec.timeout
        cmd = terraform + "providers schema -json".split(" ")
        result = self.run_process(cmd, timeout=timeout, env=env, cwd=cwd, echo=False)
        if not self._handle_result(task, result, cwd, env=env):
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

    def _get_outputs(self, task: TaskView) -> List[str]:
        outputs = [
            cast(str, p.name)
            for p in task.target.template.attributeDefs.values()
            if p.schema.get("metadata", {}).get(self.attribute_output_metadata_key)
            or p.schema.get("metadata", {}).get(tosca.ToscaOutputs._metadata_key)
        ]
        if task.configSpec.outputs:
            # allow list for backwards compatibility
            if not isinstance(task.configSpec.outputs, (dict, list)):
                raise UnfurlTaskError(
                    task,
                    f'Invalid Terraform outputs specified "{task.configSpec.outputs}"',
                )
            outputs.extend(task.configSpec.outputs)
        return outputs

    def _get_tfvars(self, task: TaskView):
        tfvars = task.inputs.get_copy("tfvars")
        if not isinstance(tfvars, str):
            tfprops = task.inputs.get_copy("arguments", {})
            # old way:
            tfprops.update(
                task._get_inputs_from_properties(task.target.attributes, "tfvar")
            )
            if isinstance(tfvars, dict):
                tfprops.update(tfvars)  # inputs override properties
            return tfprops
        # note: if tfvars is a string, metadata mapping is ignored
        return tfvars

    def _prepare_workspace(self, task: TaskView, cwd: WorkFolder):
        """
        In terraform directory:
            Write out tf.json if necessary.
        """
        # generated tf.json get written to as main.unfurl.tmp.tf.json
        write_vars = True
        contents = None
        main, main_path = self._get_main_path(task)
        if task._errors:
            main = None  # assume render failed
        if main_path:
            # it's a directory -- if difference from cwd, treat location as a module to call
            relpath = cwd.relpath_to_current(main_path)
            if relpath != ".":
                write_vars = False
                outputs = self._get_outputs(task)
                tfvars = self._get_tfvars(task)
                path, contents = generate_main(relpath, tfvars, outputs)
        else:
            if isinstance(main, str):  # assume its HCL
                contents = main
                path = "main.unfurl.tmp.tf"
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

    def _get_main_path(self, task: TaskView) -> Tuple[Any, Optional[str]]:
        """Override to set main as FilePath before checking digest."""
        main = task.inputs.get_copy("main")
        if not main:
            main = get_path(task.inputs.context, task.target.name, "src")
            if not os.path.exists(main):
                raise UnfurlTaskError(
                    task,
                    f'Input parameter "main" not specified and default terraform module directory does not exist at "{main}"',
                )
        if isinstance(main, str) and "\n" not in main:
            # if one line, assume its a path string, not inline HCL
            if not os.path.isabs(main):
                main = get_path(task.inputs.context, main, "src")
            if os.path.exists(main):
                result = task.inputs._attributes["main"]
                if not isinstance(result, Result) or not result.external:
                    task.inputs["main"] = _abspath(task.inputs.context, main)
                return None, main
            else:
                raise UnfurlTaskError(
                    task, f'Terraform module directory "{main}" does not exist'
                )
        return main, None

    def check_digest(self, task: TaskView, changeset) -> bool:
        self._get_main_path(task)
        return super().check_digest(task, changeset)

    def _prepare_vars(self, task: TaskView, cwd):
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

    def _get_workfolder_name(self, task: TaskView) -> str:
        return (
            task.inputs.get("stateLocation") or Folders.secrets
        )  # XXX global option for secrets

    def _prepare_state(
        self, task: TaskView, cwd
    ) -> Tuple[str, Optional[Mapping[str, Any]]]:
        """Write the instance's saved state to ``cwd`` and return its file name and contents."""
        # the terraform state file is associate with the current instance
        # read the (possible encrypted) version from the repository
        # and write out it as plaintext json into the local directory
        folderName = self._get_workfolder_name(task)
        if folderName == "remote":  # don't use local state file
            return "", None
        yamlPath = task.set_work_folder(folderName).permanent_path(
            "terraform.tfstate.yaml", False
        )
        state = None
        if os.path.exists(yamlPath):
            task.logger.debug("Found existing terraform.tfstate file at %s", yamlPath)
            # if exists in home, load and write out state file as json
            with open(yamlPath, "r") as f:
                state = task._manifest.yaml.load(f.read())
            cwd.write_file(state, "terraform.tfstate")
        else:
            task.logger.debug("Couldn't find terraform.tfstate file at %s", yamlPath)
        return "terraform.tfstate", state

    def _warn_migration(
        self, task: TaskView, state: Optional[Mapping[str, Any]], remote: bool
    ) -> None:
        """Warn, once per instance per job, that this run migrates the instance's state to OpenTofu."""
        key = (task.job.changeId if task.job else "", task.target.key)
        if key in _migration_warned:
            return
        lock_file = task.set_work_folder(Folders.artifacts).permanent_path(
            ".terraform.lock.hcl", False
        )
        if not _needs_migration(state, lock_file):
            return
        _migration_warned.add(key)
        if remote:
            undo = (
                "Unfurl doesn't hold this state, so it can't be reverted with git. "
                "Back it up first (e.g. `terraform state pull > backup.tfstate`) if you may need to go back."
            )
        else:
            undo = (
                "To undo before any infrastructure changes, `git revert` the commit that updates terraform.tfstate.yaml "
                "and .terraform.lock.hcl. If infrastructure has changed since, run "
                "`terraform state replace-provider registry.opentofu.org/<ns>/<type> registry.terraform.io/<ns>/<type>` "
                "for each provider."
            )
        logger.warning(
            "This run migrates %s from Terraform to OpenTofu. OpenTofu rewrites the provider addresses in its "
            "Terraform state and lock file (registry.terraform.io → registry.opentofu.org) when it next applies, "
            "and older Unfurl versions can't run the migrated deployment reliably (see the upgrade notes). %s",
            task.target.name,
            undo,
        )

    def _get_plan_path(self, task, cwd: WorkFolder):
        # the terraform state file is associate with the current instance
        # read the (possible encrypted) version from the repository
        # and write out it as plaintext json into the local directory
        jobId = task.get_job_id(task.changeId)
        return cwd.get_current_path(jobId + ".plan")

    def render(self, task: TaskView):
        workdir = task.inputs.get("workdir") or Folders.tasks
        if task.dry_run:
            dryrun_mode = task.inputs.get("dryrun_mode", "plan")
            if dryrun_mode == "real":
                task.dry_run = False
        cwd = task.set_work_folder(workdir, preserve=True)

        _, terraformcmd = self._cmd(
            task.inputs.get("command", self._default_cmd), task.inputs.get("keeplines")
        )

        cli = _check_tf_cli(task, terraformcmd[0])

        # write out any needed files to cwd, eg. main.tf.json
        mainpath, varfilePath = self._prepare_workspace(task, cwd)
        # write the state file to local if necessary
        statePath, state = self._prepare_state(task, cwd)
        if cli and cli[0] == "tofu":
            self._warn_migration(task, state, not statePath)

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

        return [cmd, terraformcmd, statePath, cli]

    def _ensure_provider_schema(
        self,
        task: TaskView,
        terraform: List[str],
        cwd: WorkFolder,
        env: Dict[str, str],
        *,
        cache_dir: str,
    ) -> Dict[str, Any]:
        """Run ``terraform init`` and return the schemas of the locked providers,
        fetching and caching them in ``cache_dir`` if a locked version isn't cached.
        Raises :class:`UnfurlTaskError` if either command fails."""
        if not self._run_init(task, terraform, cwd, env):
            raise UnfurlTaskError(task, f"terraform init failed in {cwd.cwd}")
        # only needed to encrypt sensitive values in a state file saved in cleartext
        if self._get_workfolder_name(task) in ["remote", "secrets"]:
            return {}
        locked = _locked_providers(cwd.get_current_path(".terraform.lock.hcl", False))
        schemas: Dict[str, Any] = {}
        missing: Dict[str, str] = {}
        for address, version in locked.items():
            schema = _load_schema_cache(_schema_cache_file(cache_dir, address, version))
            if schema:
                schemas[address] = schema
            else:
                missing[address] = version
        if not missing:
            return schemas
        # returns the schemas of the versions installed here, i.e. the locked ones
        fetched = self._fetch_provider_schemas(task, terraform, cwd, env)
        if fetched is None:
            raise UnfurlTaskError(
                task, f"terraform providers schema failed in {cwd.cwd}"
            )
        for address, version in missing.items():
            schema = fetched.get(address)
            if schema:
                path = _schema_cache_file(cache_dir, address, version)
                _save_schema_cache(path, schema)
                schemas[address] = schema
        return schemas

    def _run_terraform_cmd(
        self,
        task: TaskView,
        cmd: List[str],
        env: Dict[str, str],
        cwd: WorkFolder,
        *,
        background: bool,
        echo_args: Dict[str, Any],
    ):
        """Run a terraform command via :meth:`_dispatch_run`. On cancel /
        timeout, log and yield ``done(success=False)`` then return ``None``
        (caller should ``return`` immediately). Otherwise return the result."""
        result = yield from self._dispatch_run(
            task,
            cmd,
            background=background,
            timeout=task.configSpec.timeout,
            env=env,
            cwd=cwd.cwd,
            lock_cwd=True,
            **echo_args,
        )
        if result.error or result.timeout:
            # cancelled / timed out — don't try to apply a partial state file
            self._handle_result(task, result, cwd.cwd, (0, 2), env)
            yield self.done(task, success=False, result=result.__dict__)
            return None
        return result

    def _interpret_result(
        self,
        task: TaskView,
        result,
        env: Dict[str, str],
        cwd: WorkFolder,
    ) -> Tuple[bool, Optional[Status], bool]:
        """Map a terraform plan/apply result to ``(success, status, modified)``.

        ``terraform plan -detailed-exitcode`` returns 2 to mean "succeeded with
        a diff", which we treat as success. Status is derived from the
        operation kind plus what terraform reported in stdout.
        """
        success = self._handle_result(task, result, cwd.cwd, (0, 2), env)
        status: Optional[Status] = None
        needs_changes = False
        if result.returncode == 2:
            success = True  # plan succeeded despite non-zero return code
            # outputs marked sensitive always show as changed, so also look
            # for an explicit "Plan: ..." line to know if real changes apply
            needs_changes = "Plan:" in result.stdout
            if task.configSpec.operation != "check":
                status = Status.ok
        if success:
            if task.configSpec.operation == "check":
                if needs_changes:
                    if "0 to change, 0 to destroy" in result.stdout:
                        # terraform would only add resources → treat as absent
                        status = Status.absent
                    else:
                        status = Status.degraded
                elif task.target.status in (Status.pending, Status.unknown):
                    status = Status.ok
            elif task.configSpec.operation != "delete":
                status = Status.ok
        modified = any(
            marker in result.stdout
            for marker in ("Modifying...", "Creating...", "Destroying...")
        )
        return success, status, modified

    def run(self, task: TaskView):
        cwd = task.get_work_folder(Folders.tasks)
        cmd, terraform, statePath, cli = task.rendered
        # Per-task TF_DATA_DIR isolates each task's .terraform/ so per-cwd
        # init state (modules, backend, lock.hcl) cannot collide across tasks.
        dataDir = os.path.join(cwd.cwd, ".terraform")
        env = _get_env(task.environ, task.verbose, dataDir, cli)
        # Shared TF_PLUGIN_CACHE_DIR (set explicitly, via the CLI config file, or
        # to the default location by _get_env) caches provider binaries
        # across tasks: cold init writes there, warm init just symlinks into
        # the per-cwd .terraform/. We serialize init invocations on the
        # cache directory (concurrent writers race on the binary file).
        # plan/apply runs without the lock — terraform reads/executes the
        # cached binary, which Linux allows concurrently across processes.
        pluginCache = env.get("TF_PLUGIN_CACHE_DIR")
        if pluginCache:
            os.makedirs(pluginCache, exist_ok=True)
        echo_args = get_echo_args(task.verbose)
        background = bool(
            task.inputs.get("background")
            or os.environ.get("UNFURL_TEST_SHELL_BACKGROUND")
        )

        # Cache the provider schema in the shared plugin cache (if set) so
        # subsequent tasks can skip the `terraform providers schema -json`
        # call. Falls back to the per-cwd dataDir when no shared cache exists.
        schema_dir = pluginCache or dataDir
        cache_dir = _schema_cache_dir(schema_dir, terraform)

        if pluginCache:
            yield from task.acquire_path(pluginCache)
        try:
            # this cwd still needs its own init (per-cwd .terraform/);
            # a warm plugin cache makes this fast (just symlinks)
            providerSchema = self._ensure_provider_schema(
                task, terraform, cwd, env,
                cache_dir=cache_dir,
            )
        finally:
            if pluginCache:
                task.release_path(pluginCache)

        result = yield from self._run_terraform_cmd(
            task, cmd, env, cwd,
            background=background, echo_args=echo_args,
        )
        if result is None:
            return

        if result.returncode and _needs_init(clean_output(result.stderr)):
            # modules / plugins out of date — re-init under the lock and retry
            if pluginCache:
                yield from task.acquire_path(pluginCache)
            try:
                providerSchema = self._ensure_provider_schema(
                    task, terraform, cwd, env,
                    cache_dir=cache_dir,
                )
            finally:
                if pluginCache:
                    task.release_path(pluginCache)
            result = yield from self._run_terraform_cmd(
                task, cmd, env, cwd,
                background=background, echo_args=echo_args,
            )
            if result is None:
                return

        success, status, modified = self._interpret_result(task, result, env, cwd)

        if task.dry_run:
            outputs = task.inputs.get("dryrun_outputs")
            if outputs is not None:
                mock_state = dict(outputs=outputs, success=success, modified=modified)
                errors, new_status = self.process_result_template(task, mock_state)
                success = True
                if new_status is not None:
                    status = new_status
        elif task.configSpec.operation != "check":
            outputs, errors, new_status = self._apply_state(
                task, statePath, cwd, providerSchema, result, success, modified
            )
            if success and errors is not None:
                success = not errors
            if new_status is not None:
                status = new_status
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

    def _apply_state(
        self,
        task: TaskView,
        statePath: str,
        cwd: WorkFolder,
        providerSchema,
        result,
        success,
        modified,
    ):
        # read state file
        current_path = cwd.cwd
        if statePath and os.path.isfile(os.path.join(current_path, statePath)):
            statePath = os.path.join(current_path, statePath)
            with open(statePath) as sf:
                state = json.load(sf)
            state = mark_sensitive(providerSchema, state, task, self.sensitive_names)
            # save state file in home as yaml, encrypting sensitive values
            folderName = self._get_workfolder_name(task)
            if folderName != "remote":  # don't use local state file
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
            return outputs, errors, new_status
        else:
            return {}, None, None


# XXX implement discover:
# terraform import -allow-missing-config {type.name}
# convert resource schemas to TOSCA types?
# see https://www.terraform.io/docs/extend/schemas/schema-types.html#types
# types: string int float64 list set map
# behaviors: Default optional required computed (=> attribute) ForceNew: instance-key, sensitive
