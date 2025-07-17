# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
import fnmatch
from functools import partial
import io
import os.path
from pathlib import Path
import sys
import codecs
import json
import os
from typing import (
    Any,
    Mapping,
    Optional,
    TextIO,
    Union,
    Tuple,
    List,
    cast,
    TYPE_CHECKING,
    Dict,
    overload,
)
from typing_extensions import Literal
import urllib
import urllib.request
from urllib.parse import urljoin, urlparse, urlsplit
import ssl
import certifi
import git
from jsonschema import RefResolver
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.representer import RepresenterError, SafeRepresenter
from ruamel.yaml.constructor import ConstructorError

from .lock import Lock

from .util import (
    UnfurlBadDocumentError,
    UnfurlSchemaError,
    is_relative_to,
    to_bytes,
    to_text,
    sensitive_str,
    sensitive_bytes,
    sensitive_dict,
    sensitive_list,
    unique_name,
    wrap_sensitive_value,
    UnfurlError,
    find_schema_errors,
    get_base_dir,
)
from .merge import (
    expand_doc,
    make_map_with_base,
    find_anchor,
    _cache_anchors,
    restore_includes,
)
from .repo import (
    Repo,
    RepoView,
    add_user_to_url,
    normalize_git_url_hard,
    split_git_url,
    memoized_remote_tags,
)
from .packages import (
    PackageSpec,
    UnfurlPackageUpdateNeeded,
    extract_package,
    find_canonical,
    get_package_from_url,
    resolve_package,
    is_semver,
    normalize_url,
)
from . import DEFAULT_CLOUD_SERVER

from .logs import getLogger
from toscaparser.common.exception import URLException, ExceptionCollector
from toscaparser.utils.gettextutils import _
import toscaparser.imports
import tosca.loader
from toscaparser.repositories import Repository
from toscaparser.elements.interfaces import OperationDef

from ansible.parsing.vault import VaultLib, VaultSecret
from ansible.parsing.yaml.objects import AnsibleMapping, AnsibleUnicode, AnsibleSequence
from ansible.parsing.yaml.loader import AnsibleLoader, AnsibleConstructor
from ansible.utils.unsafe_proxy import AnsibleUnsafeText, AnsibleUnsafeBytes
from time import perf_counter
from jinja2.runtime import DebugUndefined

if TYPE_CHECKING:
    from .manifest import Manifest

logger = getLogger("unfurl")

try:
    from .solver import solve_topology
except ImportError:
    solve_topology = None  # type: ignore
    logger.warning(
        "Failed to import tosca_solver extension, topology inference is disabled."
    )

yaml_perf = 0.0

CLEARTEXT_VAULT = VaultLib(secrets=[("cleartext", b"")])


def yaml_dict_type(readonly: bool) -> type:
    if readonly:
        return AnsibleMapping
    else:
        return CommentedMap


def load_yaml(yaml, stream, path=None, readonly: bool = False):
    global yaml_perf
    start_time = perf_counter()
    if not readonly:
        if path and isinstance(stream, str):
            stream = io.StringIO(stream)
            stream.name = path
        doc = yaml.load(stream)
    else:
        loader = AnsibleLoader(stream, path, yaml.constructor.vault.secrets)
        loader.vault = yaml.constructor.vault
        doc = loader.get_single_data()
    yaml_perf += perf_counter() - start_time
    return doc


def _use_clear_text(vault):
    clear_id = CLEARTEXT_VAULT.secrets[0][0]
    return vault.secrets and all(s[0] == clear_id for s in vault.secrets)


def represent_undefined(dumper, data):
    msg = f"cannot represent an object: <{data}> of type {type(data)}"
    if isinstance(data, DebugUndefined):
        return dumper.represent_scalar("tag:yaml.org,2002:str", repr(msg))
    else:
        raise RepresenterError(msg)


def _represent_sensitive(dumper, data, tag):
    if dumper.vault.secrets:
        b_ciphertext = dumper.vault.encrypt(data)
        return dumper.represent_scalar(tag, b_ciphertext.decode(), style="|")
    else:
        return dumper.represent_scalar(
            "tag:yaml.org,2002:str", sensitive_str.redacted_str
        )


def represent_sensitive_str(dumper, data):
    if _use_clear_text(dumper.vault):
        return SafeRepresenter.represent_str(dumper, data)
    return _represent_sensitive(dumper, data, "!vault")


def represent_sensitive_list(dumper, data):
    if _use_clear_text(dumper.vault):
        return SafeRepresenter.represent_list(dumper, data)
    return _represent_sensitive(dumper, json.dumps(data, sort_keys=True), "!vault-json")


def represent_sensitive_dict(dumper, data):
    if _use_clear_text(dumper.vault):
        return SafeRepresenter.represent_dict(dumper, data)
    return _represent_sensitive(dumper, json.dumps(data, sort_keys=True), "!vault-json")


def represent_sensitive_bytes(dumper, data):
    if _use_clear_text(dumper.vault):
        return SafeRepresenter.represent_binary(dumper, data)
    return _represent_sensitive(dumper, data, "!vault-binary")


def _construct_vault(constructor, node, tag):
    value = constructor.construct_scalar(node)
    if getattr(constructor.vault, "skip_decode", False):
        return sensitive_str.redacted_str
    if not constructor.vault.secrets:
        raise ConstructorError(
            context=None,
            context_mark=None,
            problem=f"found {tag} but no vault password provided",
            problem_mark=node.start_mark,
            note=None,
        )
    return value


def construct_vault(constructor, node):
    value = _construct_vault(constructor, node, "!vault")
    if value is sensitive_str.redacted_str:
        return value
    cleartext = to_text(constructor.vault.decrypt(value))
    return sensitive_str(cleartext)


def construct_vaultjson(constructor, node):
    value = _construct_vault(constructor, node, "!vault-json")
    if value is sensitive_str.redacted_str:
        return value
    cleartext = to_text(constructor.vault.decrypt(value))
    return wrap_sensitive_value(json.loads(cleartext))


def construct_vaultbinary(constructor, node):
    value = _construct_vault(constructor, node, "!vault-binary")
    if value is sensitive_str.redacted_str:
        return value
    cleartext = constructor.vault.decrypt(value)
    return sensitive_bytes(cleartext)


AnsibleConstructor.add_constructor("!vault", construct_vault)
AnsibleConstructor.add_constructor("!vault-json", construct_vaultjson)
AnsibleConstructor.add_constructor("!vault-binary", construct_vaultbinary)


def make_yaml(vault=None):
    if not vault:
        vault = VaultLib(secrets=None)
    yaml = YAML()
    yaml.preserve_quotes = True  # type:ignore[assignment]

    # monkey patch for better error message
    yaml.representer.represent_undefined = represent_undefined
    yaml.representer.add_representer(None, represent_undefined)

    yaml.constructor.vault = vault
    yaml.constructor.add_constructor("!vault", construct_vault)
    yaml.constructor.add_constructor("!vault-json", construct_vaultjson)
    yaml.constructor.add_constructor("!vault-binary", construct_vaultbinary)

    yaml.representer.vault = vault
    # write out <<REDACTED>> or encrypt depending on vault.secrets
    yaml.representer.add_representer(sensitive_str, represent_sensitive_str)
    yaml.representer.add_representer(sensitive_dict, represent_sensitive_dict)
    yaml.representer.add_representer(sensitive_list, represent_sensitive_list)
    yaml.representer.add_representer(sensitive_bytes, represent_sensitive_bytes)

    yaml.representer.add_representer(AnsibleUnsafeText, SafeRepresenter.represent_str)
    yaml.representer.add_representer(
        AnsibleUnsafeBytes, SafeRepresenter.represent_binary
    )
    yaml.representer.add_representer(AnsibleUnicode, SafeRepresenter.represent_str)
    yaml.representer.add_representer(AnsibleSequence, SafeRepresenter.represent_list)
    yaml.representer.add_representer(AnsibleMapping, SafeRepresenter.represent_dict)
    return yaml


yaml = make_yaml()
cleartext_yaml = make_yaml(CLEARTEXT_VAULT)


class UnfurlVaultLib(VaultLib):
    skip_decode = False


def make_vault_lib(
    passwordBytes: Union[str, bytes], vaultId="default"
) -> Optional[VaultLib]:
    if passwordBytes:
        if isinstance(passwordBytes, str):
            passwordBytes = to_bytes(passwordBytes)
        vault_secrets = [(vaultId, VaultSecret(passwordBytes))]
        return VaultLib(secrets=vault_secrets)
    return None


def make_vault_lib_ex(secrets: List[Tuple[str, Union[str, bytes]]]) -> VaultLib:
    vault_secrets = []
    for vaultId, passwordBytes in secrets:
        if isinstance(passwordBytes, str):
            passwordBytes = to_bytes(passwordBytes)
        vault_secrets.append((vaultId, VaultSecret(passwordBytes)))
    return VaultLib(secrets=vault_secrets)


def urlopen(url):
    return urllib.request.urlopen(
        url, context=ssl.create_default_context(cafile=certifi.where())
    )


_refResolver = RefResolver("", None)

# context for the resolved url or local path: is_file, repo_view, base url or path, file_name
ImportResolver_Context = Tuple[bool, Optional[RepoView], str, str]


def match_namespace(packages: str, namespace_id: str):
    for p in packages.split():
        if fnmatch.fnmatch(namespace_id, p):
            start, sep, end = p.partition("*")
            return start or "global"
    return ""


class ImportResolver(toscaparser.imports.ImportResolver):
    """
    This class is used to resolve imports and artifacts by the YAML TOSCA parser and by the Python import loader.

    Each imported service template can contain TOSCA repository definitions and its corresponding namespace maintains a list of repositories in its scope,
    so it is okay if imports declare different repositories with the same name.

    However, Python package names are global to the app so Python imports like ``tosca_repositories.<repository name>`` will resolve to the latest TOSCA repository registered with that name.
    Therefore, it is recommended that public blueprints give repositories unique names.
    """

    safe_mode: bool = False

    def __init__(
        self, manifest: "Manifest", ignoreFileNotFound=False, expand=False, config=None
    ):
        self.manifest = manifest
        self.readonly = bool(
            manifest and manifest.localEnv and manifest.localEnv.readonly
        )
        self.ignoreFileNotFound = ignoreFileNotFound
        self.yamlloader = manifest.loader if manifest else None
        self.expand = expand
        self.config = config or {}
        if manifest:
            self.solve_topology = self._solve_topology
        else:
            self.solve_topology = solve_topology

    def _solve_topology(self, topology_template):
        if solve_topology is not None:
            constrain_required = not is_semver(
                self.manifest.apiVersion[len("unfurl/") :]
            )  # original version needs this
            return solve_topology(
                topology_template, self.manifest.resolve_select, constrain_required
            )
        else:
            for node_template in topology_template.node_templates.values():
                if "select" in node_template.directives:
                    self.manifest.resolve_select(node_template)

    GLOBAL_NAMESPACE_PACKAGES = os.getenv(
        "UNFURL_GLOBAL_NAMESPACE_PACKAGES",
        "unfurl.cloud/onecommons/unfurl-types* gitlab.com/onecommons/unfurl-types*",
    )

    def __getstate__(self):
        state = self.__dict__.copy()
        state["yamlloader"] = None
        return state

    def get_safe_mode(self) -> bool:
        return tosca.loader.get_safe_mode(
            (self.manifest and self.manifest.safe_mode) or self.safe_mode
        )

    def load_imports(
        self,
        importsLoader: toscaparser.imports.ImportsLoader,
        importslist: List[Union[str, Dict]],
    ):
        while True:
            try:
                return super().load_imports(importsLoader, importslist)
            except UnfurlPackageUpdateNeeded:
                pass  # reload

    def find_matching_node(self, relTpl, req_name, req_def):
        if self.manifest and self.manifest.tosca:
            return self.manifest.tosca.find_matching_node(relTpl, req_name, req_def)
        else:
            return super().find_matching_node(relTpl, req_name, req_def)

    def find_implementation(self, op: OperationDef) -> Optional[Dict[str, Any]]:
        from .planrequests import _get_config_spec_args_from_implementation
        from . import (
            configurators,
        )  # need to import configurators to get short names  # noqa: F401
        from .spec import NodeSpec

        inputs = op.inputs if op.inputs is not None else {}
        node = None
        if self.manifest:
            if not op._source:
                op._source = self.manifest.get_base_dir()
            if self.manifest.tosca and self.manifest.tosca.topology:
                if op.node_template:
                    node_template = op.node_template
                else:
                    ntype = op.ntype.type if op.ntype else "tosca:Root"
                    topology = self.manifest.tosca.topology.topology_template
                    nname = f"_{ntype}"
                    node_template = topology.add_node_template(
                        nname, dict(type=ntype, imported="ignore")
                    )
                    del topology.node_templates[nname]
                node = NodeSpec(node_template, self.manifest.tosca.topology)
        return cast(
            Optional[Dict[str, Any]],
            _get_config_spec_args_from_implementation(
                op, inputs, node, None, False, self.get_safe_mode(), False
            ),
        )

    def _match_repoview(
        self, name: str, tpl: Optional[Dict[str, Any]]
    ) -> Optional[RepoView]:
        if not self.manifest:
            return None
        if tpl:  # match by url
            # normalize_git_url_hard removes the fragment
            url = tpl["url"]
            package = get_package_from_url(url)
            if package:
                PackageSpec.update_package(self.manifest.package_specs, package)
                url = package.url
            url = normalize_git_url_hard(url)
        else:
            url = None
        candidate = None
        for repo_name, repo_view in self.manifest.repositories.items():
            if url:  # match by url
                if url == normalize_git_url_hard(repo_view.url):
                    # XXX repo_view.revision == tpl.get("revision")
                    candidate = repo_view
            if candidate or not url:
                if repo_name == name or repo_view.python_name == name:
                    break
        else:
            if candidate:  # use candidate even though name didn't match
                repo_view = candidate
            else:
                return None  # no match
        self._resolve_repoview(repo_view)
        return repo_view

    def find_repository_path(
        self,
        name: str,
        tpl: Optional[Dict[str, Any]] = None,
        base_path: Optional[str] = None,
    ) -> Optional[str]:
        "Called by tosca.loader to resolve tosca_repositories python packages"
        repo_view = self._match_repoview(name, tpl)
        if not repo_view:
            logger.debug("Could not find a repository for '%s' (%s)", name, tpl)
            return None
        return self._get_link_to_repo(repo_view, base_path)

    def _get_link_to_repo(
        self, repo_view: RepoView, base_path: Optional[str]
    ) -> Optional[str]:
        project_base_path = (
            self.manifest.localEnv.project.projectRoot
            if self.manifest.localEnv and self.manifest.localEnv.project
            else self.manifest.get_base_dir()
        )
        # this will clone the repo if needed:
        self._resolve_repo_to_path(repo_view, project_base_path, "")
        assert repo_view.repo
        # # XXX this will use the wrong RepoView if url has path fragment
        link_name, target_path = repo_view.get_link(base_path or project_base_path)
        if link_name:
            return target_path
        return None

    @overload
    def get_repository(
        self, name: str, tpl: None, unique: Literal[False] = False
    ) -> Optional[Repository]: ...

    @overload
    def get_repository(
        self, name: str, tpl: dict, unique: bool = False
    ) -> Repository: ...

    def get_repository(
        self, name: str, tpl: Optional[dict] = None, unique: bool = False
    ) -> Optional[Repository]:
        # this is called by ToscaTemplate
        # may modify tpl
        if self.manifest:
            if not unique and name in self.manifest.repositories:
                # don't create another Repository instance
                return self.manifest.repositories[name].repository
            else:
                name = unique_name(name, list(self.manifest.repositories))

        if tpl is None:
            return None

        return self._create_repository(name, tpl)

    def _create_repository(self, name: str, tpl: dict) -> Repository:
        if isinstance(tpl, dict):
            if "url" in tpl:
                url = tpl["url"]
                if (
                    "://" not in url and "@" in url
                ):  # scp style used by git: user@server:project.git
                    # convert to ssh://user@server/project.git
                    url = "ssh://" + url.replace(":", "/", 1)
                tpl["url"] = url

            if tpl.get("credential") and self.manifest and not self.manifest.safe_mode:
                credential = tpl["credential"]
                # support expressions to resolve credential secrets
                if self.manifest.rootResource:
                    from .eval import map_value

                    tpl["credential"] = map_value(
                        credential, self.manifest.rootResource
                    )
                elif self.manifest.localEnv:
                    # we're including or importing before we finished initializing
                    context = self.manifest.localEnv.get_context(
                        self.config.get("environment")
                    )
                    tpl["credential"] = self.manifest.localEnv.map_value(
                        credential, cast(dict, context.get("variables"))
                    )
                tpl["credential"] = wrap_sensitive_value(tpl["credential"])
        elif isinstance(tpl, str):
            tpl = dict(url=tpl)
        else:
            raise UnfurlError(
                f'invalid repository "{name}": definition {tpl} is type {type(tpl)}, not a map or string'
            )
        return Repository(name, tpl)

    def _compare_repository_urls(self, r_url: str, url: str) -> Tuple[bool, str, str]:
        # raise error if a different repository with the same name already exists
        if r_url != url:
            r_url = normalize_url(r_url)
            url = normalize_url(url)
            if r_url != url:
                return False, r_url, url
        return True, "", ""

    def add_repository(self, name: str, tpl: dict) -> Repository:
        # called by tosca.Repository.__init__() to register itself
        if not self.manifest:
            return self.get_repository(name, tpl)
        repo: Optional[RepoView] = self.manifest.repositories.get(name)
        if repo:
            eq, existing_url, new_url = self._compare_repository_urls(
                repo.repository.url, tpl["url"]
            )
            if eq:
                return repo.repository
            # url doesn't match, rename existing before its replaced
            if existing_url not in self.manifest.repositories:
                self.manifest.repositories[existing_url] = repo
                logger.warning("replacing repository %s with %s", name, new_url)
                logger.debug(
                    "renaming existing repository %s to %s", name, existing_url
                )
        repository = self._create_repository(name, tpl)
        self.manifest.repositories[name] = RepoView(repository, None, "")
        logger.debug("adding repository %s", name)
        return repository

    def get_repository_url(
        self,
        importsLoader,
        repository_name,
        source_info: Optional[toscaparser.imports.SourceInfo] = None,
    ) -> str:
        if repository_name:
            if repository_name not in importsLoader.repositories:
                importsLoader.repositories[repository_name] = (
                    self.manifest.repositories[repository_name].repository.tpl
                )
            url = super().get_repository_url(importsLoader, repository_name)
        else:
            if self.manifest:
                if self.manifest.repo:
                    url = self.manifest.repo.get_url_with_path(
                        self.manifest.get_base_dir(), True
                    )
                else:
                    url = self.manifest.path or ""
            else:
                url = ""
        if source_info:
            return self._get_namespace_from_source_info(url, source_info)
        else:
            return url

    def _get_namespace_from_source_info(
        self, url, source_info: toscaparser.imports.SourceInfo
    ) -> str:
        from .graphql import get_namespace_id

        path = source_info["path"]
        if path and not toscaparser.imports.is_url(path):
            # handle case were type was ?include-d not imported
            repo_view = self._find_repoview_from_path(path)
            if repo_view:
                url = repo_view.url
                relfile = os.path.normpath(
                    Path(source_info["path"]).relative_to(repo_view.working_dir)
                )
                if relfile != ".":
                    source_info["file"] = relfile
        namespace_id = get_namespace_id(url, source_info)
        if self.manifest and self.manifest.package_specs:
            canonical = urlparse(DEFAULT_CLOUD_SERVER).hostname
            if canonical:
                namespace_id = find_canonical(
                    self.manifest.package_specs, canonical, namespace_id
                )

        explicit_namespace = match_namespace(
            self.GLOBAL_NAMESPACE_PACKAGES, namespace_id
        )
        if explicit_namespace:
            namespace_id = explicit_namespace
            source_info["namespace_uri"] = explicit_namespace
        return namespace_id

    confine_user_paths = True

    def _has_path_escaped_relative(self, path, repository_name="") -> bool:
        if not self.confine_user_paths:
            return False
        if os.path.normpath(path).startswith("..") or os.path.isabs(path):
            msg = f'Path not allowed outside of repository {repository_name}: "{path}"'
            ExceptionCollector.appendException(ImportError(msg))
            return True
        else:
            return False

    def _has_path_escaped_base(self, path: str, base) -> bool:
        if not self.confine_user_paths:
            return False
        if not base:
            return self._has_path_escaped_project(path)

        # relative to file
        absbase = os.path.abspath(base)
        if not absbase[-1] != "/":
            absbase += "/"
        if not os.path.abspath(path).startswith(absbase):
            msg = f'Path not allowed outside of repository or document root "{absbase}": "{path}"'
            ExceptionCollector.appendException(ImportError(msg))
            return True
        else:
            return False

    def _has_path_escaped_project(self, path) -> bool:
        if not self.confine_user_paths:
            return False

        # user supplied path can't be outside of the project or the home project
        if self.manifest.localEnv and self.manifest.localEnv.project:
            if os.path.abspath(path).startswith(
                os.path.abspath(os.path.dirname(__file__))
            ):
                # special case for built-in "unfurl" repository
                return False
            if self.manifest.localEnv.project.get_relative_path(path).startswith(".."):
                if (
                    not self.manifest.localEnv.homeProject
                    or self.manifest.localEnv.homeProject.get_relative_path(
                        path
                    ).startswith("..")
                ):
                    msg = f'Path "{os.path.abspath(path)}" not allowed outside of project: "{self.manifest.localEnv.project.projectRoot}"'
                    ExceptionCollector.appendException(ImportError(msg))
                    return True
        return False

    def _find_repoview(self, url: str) -> RepoView:
        repo_view = None
        git_url, path, revision = split_git_url(url)  # we only support git urls
        assert self.manifest.localEnv
        repoview_or_url = self.manifest.localEnv._find_git_repo(git_url, revision)
        if isinstance(repoview_or_url, RepoView):
            repo_view = repoview_or_url
        else:
            # repo wasn't not found, repoview_or_url is the git_url (with credentials possibly applied)
            # create new RepoView for this url
            name = Repo.get_path_for_git_repo(repoview_or_url, name_only=True)
            repository = self.get_repository(name, dict(url=repoview_or_url), True)
            repo_view = RepoView(repository, None)
        return repo_view

    def _resolve_repo_to_path(
        self,
        repo_view: RepoView,
        base: str,
        file_name: str,
    ) -> str:
        # XXX lock_to_commit not implemented, currently used to indicate the lock have a tag
        # commit = repo_view.package.lock_to_commit if repo_view.package else ""
        if not repo_view.repo:
            # calls LocalEnv.find_or_create_working_dir()
            # XXX coalesce repoviews
            locked = repo_view.package and repo_view.package.locked or False
            repo, file_path, revision, bare = self.manifest.find_repo_from_git_url(
                repo_view.as_git_url(), base, locked
            )
            if repo:
                repo_view.repo = repo
            else:
                raise UnfurlError(
                    "Could not resolve git URL: " + repo_view.as_git_url(True)
                )
        else:
            repo = repo_view.repo
            file_path = repo_view.path
            revision = repo.revision
            bare = False  # XXX if commit

        if bare:
            path = self._get_bare_path(repo, file_path, revision)
        else:
            path = os.path.join(repo.working_dir, file_path, file_name or "").rstrip(
                "/"
            )
        return path

    def get_remote_tags(self, url, pattern="*") -> List[str]:
        # apply credentials to url like find_repo_from_git_url() does
        if self.manifest.repo:
            candidate_parts = urlsplit(self.manifest.repo.url)
            password = candidate_parts.password
            username = candidate_parts.username
        else:
            password = ""
            username = ""
            candidate_parts = None
        if password and username:
            url_parts = urlsplit(url)
            assert candidate_parts
            if (
                candidate_parts.hostname == url_parts.hostname
                and candidate_parts.port == url_parts.port
            ):
                # rewrite url to add credentials
                url = add_user_to_url(url, username, password)
        return memoized_remote_tags(url, pattern="*")

    def _find_repository_root(self, base: str):
        assert base
        try:
            repo = git.Repo(base, search_parent_directories=True)
            return repo.working_dir
        except Exception:
            return base

    def _find_repoview_from_path(self, base: str) -> Optional[RepoView]:
        assert base
        nearest = ""
        candidate = None
        if self.manifest:
            # if repository is nested in another choose the most nested
            for repo_view in self.manifest.repositories.values():
                if not repo_view.repo:
                    if self.manifest.localEnv:
                        repo_view.repo = self.manifest.localEnv.find_git_repo(
                            repo_view.url
                        )
                if is_relative_to(base, repo_view.working_dir):
                    if len(repo_view.working_dir) > len(nearest):
                        nearest = repo_view.working_dir
                        candidate = repo_view
        return candidate

    def resolve_url(
        self,
        importsLoader: toscaparser.imports.ImportsLoader,
        base: str,
        file_name: str,
        repository_name: Optional[str],
    ) -> Tuple[Optional[str], Optional[ImportResolver_Context]]:
        # resolve to an url or absolute path along with context
        existing: Optional[RepoView] = None
        if repository_name:
            if self._has_path_escaped_relative(file_name, repository_name):
                # file_name can't be ".." or absolute path
                return None, None
            existing = repo_view = self.manifest.repositories.get(repository_name)
            tpl = importsLoader.repositories.get(repository_name)
            if repo_view and tpl:
                eq, _, normalized = self._compare_repository_urls(
                    repo_view.repository.url, tpl["url"]
                )
                if not eq:  # check if we already added this as normalized
                    repo_view = self.manifest.repositories.get(normalized)
            if not repo_view and tpl:
                repo_view = RepoView(
                    self._create_repository(repository_name, tpl), None, ""
                )
        else:
            # if file_name is relative, base will be set (to the importsLoader's path)
            if not toscaparser.imports.is_url(base):
                return self._resolve_file_path(importsLoader, base, file_name)
            repo_view = self._find_repoview(base)

        assert repo_view
        self._resolve_repoview(repo_view)  # apply package rules (idempotent)
        if repository_name:
            if existing:
                # compare again after applying package rules
                eq, normalized, _ = self._compare_repository_urls(
                    repo_view.repository.url, existing.repository.url
                )
                if not eq and normalized not in self.manifest.repositories:
                    # still not equal, use normalized as its name to avoid replacing existing
                    self.manifest.repositories[normalized] = repo_view
                    logger.debug(
                        "adding repository %s as %s to avoid replacing existing repository",
                        repository_name,
                        normalized,
                    )
            else:
                self.manifest.repositories[repository_name] = repo_view
                logger.debug(
                    "adding repository %s while resolving url", repository_name
                )

        path = toscaparser.imports.normalize_path(repo_view.url)
        is_file = not toscaparser.imports.is_url(path)
        if is_file:
            if not os.path.isabs(path):
                path = os.path.join(base, path)
            path = os.path.join(path, file_name)
            # repositories can be outside of the project when not in safe mode
            if not repository_name or self.safe_mode:
                if self._has_path_escaped_project(path):
                    return None, None
        repo_view.add_file_ref(file_name)
        return path, (is_file, repo_view, base, file_name)

    def _resolve_file_path(
        self,
        importsLoader: toscaparser.imports.ImportsLoader,
        base: str,
        file_name: str,
    ) -> Tuple[Optional[str], Optional[ImportResolver_Context]]:
        assert base or os.path.isabs(file_name), (
            f"{file_name} isn't absolute and base isn't set"
        )
        url = os.path.join(base, file_name)
        repository_root = None  # default to checking if its in the project
        if importsLoader.repository_root:
            if toscaparser.imports.is_url(importsLoader.repository_root):
                # at least make sure we didn't break out of the base
                repository_root = self._find_repository_root(base)
            else:
                repository_root = importsLoader.repository_root
        if self._has_path_escaped_base(url, repository_root):
            return None, None
        return url, (True, None, base, file_name)

    def _resolve_repoview(self, repo_view: RepoView) -> None:
        if repo_view.package is None:
            package = extract_package(repo_view)
            if not package:
                return
            locked = self.config.get("lock")  # top-level section of ensemble
            if locked:
                lock_dict = Lock.find_package(locked, self.manifest, package)
            else:
                lock_dict = None
            if os.getenv("UNFURL_SKIP_UPSTREAM_CHECK") or (
                self.manifest.localEnv
                and self.manifest.localEnv.overrides.get("UNFURL_SKIP_UPSTREAM_CHECK")
            ):
                remote_tags_check = None
            else:
                remote_tags_check = self.get_remote_tags
            # need to resolve if its a package
            # if repoview.repository references a package, set the repository's url
            # and register this reference with the package
            # might raise error if version conflict
            resolve_package(
                repo_view,
                self.manifest.packages,
                self.manifest.package_specs,
                remote_tags_check,
                lock_dict,
            )

    def resolve_to_local_path(
        self, base_dir, file_name, repository_name, repositories=None
    ) -> Tuple[Optional[str], Optional[str]]:
        # this should only be called during deploy time when an operation needs direct access to directories in the repository
        # (see ArtifactSpec.get_path_and_fragment)
        if repository_name and repositories is None:
            rv = self.manifest.repositories.get(repository_name)
            if rv:
                repository_tpl = rv.repository.tpl
                repositories = {repository_name: repository_tpl}
            else:
                repositories = (
                    self.manifest.tosca
                    and self.manifest.tosca.template.tpl.get("repositories")
                    or {}
                )
                if repository_name not in repositories:
                    return None, None
        else:
            repositories = {}
        loader = toscaparser.imports.ImportsLoader(
            None, base_dir, repositories=repositories, resolver=self
        )
        tpl = dict(file=file_name, repository=repository_name)
        url_info = loader.resolve_import(tpl)
        if url_info:
            base, url, fragment, ctx = url_info
            is_file, repo_view, base, file_name = cast(ImportResolver_Context, ctx)
            if is_file:
                # already a path
                return url, fragment
            else:
                # clone git repository if necessary
                assert repo_view  # if url must
                return (
                    self._really_resolve_to_local_path(repo_view, base, file_name),
                    fragment,
                )
        return None, None

    def _really_resolve_to_local_path(
        self,
        repo_view: RepoView,
        base: str,
        file_name: str,
    ) -> str:
        return self._resolve_repo_to_path(repo_view, base, file_name)

    def _get_bare_path(self, repo, filePath, revision):
        # # XXX support empty filePath or when filePath is a directory -- need to checkout the tree
        path = repo.working_dir
        if not filePath:
            raise UnfurlError(
                f"local repository for '{path}' can not checkout revision '{revision}'"
            )
        elif repo.repo.rev_parse(revision + ":" + filePath).type != "blob":
            raise UnfurlError(
                f"can't retrieve '{filePath}' with revision '{revision}' from local repository for '{path}'"
            )
        url = "git-ref:" + repo.url
        return f"{url}#{revision}:{filePath}"

    def _open(self, path: str, isFile: bool) -> Tuple[Optional[TextIO], bool]:
        ok_to_show = True
        if path.startswith("git-ref:"):
            # XXX we need both the commit and revision if we don't have the full git repo
            url, filePath, revision = split_git_url(path[len("git-ref:") :])
            repo_view = self._find_repoview(url)
            if not repo_view.repo:
                raise UnfurlError("Could not resolve " + path)
            bdata = repo_view.repo.show(filePath, revision, stdout_as_string=False)
            if self.yamlloader:
                # ok_to_show if bdata was cleartext otherwise it was encrypted
                bdata, ok_to_show = self.yamlloader._decrypt_if_vault_data(
                    bdata, filePath
                )
            return io.StringIO(codecs.decode(bdata)), ok_to_show
        else:
            try:
                if isFile:
                    ignoreFileNotFound = self.ignoreFileNotFound
                    if ignoreFileNotFound and not os.path.isfile(path):
                        return None, ok_to_show

                    if self.yamlloader:
                        # ok_to_show if contents was cleartext otherwise it was encrypted
                        contents, ok_to_show = self.yamlloader._get_file_contents(path)
                        f: TextIO = io.StringIO(codecs.decode(contents))
                    else:
                        f = codecs.open(path, encoding="utf-8", errors="strict")
                else:
                    f = urlopen(path)
                return f, ok_to_show
            except urllib.error.URLError as e:
                if hasattr(e, "reason"):
                    msg = _(
                        (
                            'Failed to reach server "%(path)s". Reason is: '
                            + "%(reason)s."
                        )
                    ) % {"path": path, "reason": e.reason}
                    ExceptionCollector.appendException(URLException(what=msg))
                    return None, ok_to_show
                elif hasattr(e, "code"):
                    msg = _(
                        (
                            'The server "%(path)s" couldn\'t fulfill the request. '
                            + 'Error code: "%(code)s".'
                        )
                    ) % {
                        "path": path,
                        "code": e.code,  # type: ignore
                    }
                    ExceptionCollector.appendException(URLException(what=msg))
                    return None, ok_to_show
                else:
                    raise

    def load_yaml(
        self,
        path: str,
        fragment: Optional[str],
        ctx: ImportResolver_Context,
    ) -> Tuple[Any, bool]:
        # Called by the ImportsLoader with the resolved path or url
        # ctx is set by self.resolve_url()
        is_file, repo_view, base, file_name = ctx
        if not is_file:
            # clone git repository if necessary
            assert repo_view  # urls must have a repo_view
            path = self._resolve_repo_to_path(repo_view, base, file_name)
            is_file = True
            base = repo_view.working_dir
        return self._really_load_yaml(path, is_file, fragment, repo_view, base)

    def _convert_to_yaml(
        self,
        contents: str,
        path: str,
        repo_view: Optional[RepoView],
        base_dir: str,
        yaml_dict=dict,
    ):
        from .dsl import convert_to_yaml, maybe_reconvert

        if path.endswith(".py"):
            return convert_to_yaml(self, contents, path, repo_view, base_dir, yaml_dict)
        doc = maybe_reconvert(self, contents, path, repo_view, base_dir, yaml_dict)
        if doc is not None:
            return doc
        else:
            return load_yaml(yaml, contents, path, self.readonly)

    def _really_load_yaml(
        self,
        path: str,
        isFile: bool,
        fragment: Optional[str],
        repo_view: Optional[RepoView],
        base_dir: str,
    ) -> Tuple[Any, bool]:
        originalPath = path
        try:
            logger.trace(
                "attempting to load YAML %s: %s", "file" if isFile else "url", path
            )
            if isFile and path.endswith(".yaml") and not os.path.isfile(path):
                py_path = path[: -len(".yaml")] + ".py"
                if os.path.isfile(py_path):
                    logger.verbose(
                        "%s is missing but %s was found, attempting to convert to yaml.",
                        path,
                        py_path,
                    )
                    path = py_path  # try to convert dsl py to yaml
            f, cacheable = self._open(path, isFile)
            if f is None:
                return None, cacheable

            with f:
                contents = f.read()
                yaml_dict = yaml_dict_type(self.readonly)
                if toscaparser.imports.is_url(base_dir):
                    base_dir = get_base_dir(path)
                doc = self._convert_to_yaml(
                    contents, path, repo_view, base_dir, yaml_dict
                )
                if isinstance(doc, yaml_dict):
                    if self.expand:
                        # self.expand is true when doing a TOSCA import (see Manifest._load_spec())
                        doc = YamlConfig(
                            doc,
                            get_base_dir(path),  # needs this as base_dir
                            loadHook=partial(
                                self.manifest.load_yaml_include,
                                repository_root=base_dir,
                            ),
                            readonly=self.readonly,
                        ).expanded
                    doc.path = path
                    doc.base_dir = get_base_dir(path)
            if fragment and doc:
                return _refResolver.resolve_fragment(doc, fragment), cacheable
            else:
                return doc, cacheable
        except Exception:
            if path != originalPath:
                msg = f'Could not load "{path}" (originally "{originalPath}")'
            else:
                msg = f'Could not load "{path}"'
            raise UnfurlError(msg, True)


class SimpleCacheResolver(ImportResolver):
    def set_cache(self, path, val):
        self.manifest.cache[path] = (val, 0)

    def get_cache(self, doc_key):
        cache = self.manifest.cache
        if doc_key in cache:
            val, count = cache[doc_key]
            cache[doc_key] = (val, count + 1)
            return val
        return None

    def load_yaml(
        self,
        path: str,
        fragment: Optional[str],
        ctx: ImportResolver_Context,
    ) -> Tuple[Any, bool]:
        is_file, repo_view, base, file_name = ctx
        if not is_file:
            # clone git repository if necessary
            assert repo_view  # urls must have a repo_view
            path = self._resolve_repo_to_path(repo_view, base, file_name)
        doc = self.get_cache((path, fragment))
        if doc is None:
            doc, cacheable = self._really_load_yaml(
                path, True, fragment, repo_view, base
            )
            if cacheable:
                self.set_cache((path, fragment), doc)
        else:
            cacheable = True
        return doc, cacheable


class LoadIncludeAction:
    def __init__(
        self, directive: str, check_include: bool = False, get_key: Optional[str] = None
    ):
        self.directive = directive
        self.get_key = get_key
        # check_include is called to see check if the include exists
        self.check_include = check_include


class YamlConfig:
    def __init__(
        self,
        config=None,
        path=None,
        validate=True,
        schema=None,
        loadHook=None,
        vault=None,
        readonly=False,
    ):
        try:
            self._yaml = None
            self.vault = vault
            self.path = None
            self.schema = schema
            self.readonly = bool(readonly)
            self.file_size = None
            self.saved = False
            if path:
                self.path = os.path.abspath(path)
                err_msg = f"Unable to load yaml config at {self.path}"
                if os.path.isfile(self.path):
                    statinfo = os.stat(self.path)
                    # st_mtime is unreliable so use file_size as a good-enough proxy
                    # to detect the file changing out from under us
                    self.file_size = statinfo.st_size
                    with open(self.path, "r") as f:
                        config = f.read()
                # otherwise use default config
            else:
                self.path = None
                err_msg = "Unable to parse yaml config"

            if isinstance(config, str):
                self.config: dict = load_yaml(
                    self.yaml, config, self.path, self.readonly
                )
            elif isinstance(config, dict):
                self.config = CommentedMap(config.items())
            else:
                self.config = cast(dict, config)
            if not isinstance(self.config, dict):
                raise UnfurlBadDocumentError(
                    f'{err_msg}: invalid YAML document with contents: "{self.config}"'
                )

            # schema should include defaults but can't validate because it doesn't understand includes
            # but should work most of time
            setattr(self.config, "loadTemplate", self.load_include)
            self.loadHook = loadHook
            self.baseDirs = [self.get_base_dir()]
            while True:
                try:
                    self.includes, self.expanded = self._expand()
                except UnfurlPackageUpdateNeeded:
                    pass  # reload
                else:
                    break
            errors = schema and self.validate(self.expanded)
            if errors and validate:
                (message, schemaErrors) = errors
                raise UnfurlSchemaError(
                    err_msg + ": JSON Schema validation failed: " + message,
                    errors,
                    self.config,
                )
            else:
                self.valid = not not errors
        except UnfurlBadDocumentError:
            raise
        except Exception:
            raise UnfurlBadDocumentError(err_msg, saveStack=True)

    def clone(self, validate: bool = True) -> "YamlConfig":
        # reloads the config
        return YamlConfig(
            self.config,
            self.path,
            validate,
            self.schema,
            self.loadHook,
            self.vault,
            self.readonly,
        )

    def _expand(self) -> Tuple[Mapping, Mapping]:
        find_anchor(self.config, None)  # create _anchorCache
        self._cachedDocIncludes: Dict[str, Tuple[str, dict, str]] = {}
        yaml_dict = yaml_dict_type(self.readonly)
        return expand_doc(
            self.config,
            cls=make_map_with_base(self.config, self.baseDirs[0], yaml_dict),
        )

    def load_yaml(self, path, baseDir=None, warnWhenNotFound=False):
        url = urlsplit(path)
        if url.scheme.startswith("http") and url.netloc:  # looks like an absolute url
            fragment = url.fragment
            logger.trace("attempting to load YAML url: %s", path)
            try:
                f = urlopen(path)
            except urllib.error.URLError:
                if warnWhenNotFound:
                    logger.warning(f"document include {path} could not be retrieved")
                    return path, None
                raise
        else:
            path, sep, fragment = path.partition("#")
            path = os.path.abspath(os.path.join(baseDir or self.get_base_dir(), path))
            if warnWhenNotFound and not os.path.isfile(path):
                logger.warning(
                    f"document include {path} does not exist (base: {baseDir})"
                )
                return path, None
            logger.trace("attempting to load YAML file: %s", path)
            f = open(path, "r")

        with f:
            config = load_yaml(self.yaml, f, path, self.readonly)
        if fragment and config:
            return path, _refResolver.resolve_fragment(config, fragment)
        return path, config

    def get_base_dir(self):
        if self.path:
            return get_base_dir(self.path)
        else:
            return "."

    def restore_includes(self, changed):
        restore_includes(self.includes, self.config, changed, cls=CommentedMap)

    def dump(self, out=sys.stdout):
        try:
            self.yaml.dump(self.config, out)
        except Exception:
            raise UnfurlError(f"Error saving {self.path}", True)

    def save(self):
        if self.readonly:
            raise UnfurlError(f'Can not save "{self.path}", it is set to readonly')
        output = io.StringIO()
        self.dump(output)
        if self.path:
            if self.file_size:
                statinfo = os.stat(self.path)
                if statinfo.st_size > self.file_size:
                    logger.error(
                        'Not saving "%s", it was unexpectedly modified after it was loaded, %d is after last modified time %d',
                        self.path,
                        statinfo.st_size,
                        self.file_size,
                    )
                    raise UnfurlError(
                        f'Not saving "{self.path}", it was unexpectedly modified after it was loaded'
                    )
            with open(self.path, "w") as f:
                f.write(output.getvalue())
            statinfo = os.stat(self.path)
            self.file_size = statinfo.st_size
            self.saved = True
        return output

    @property
    def yaml(self):
        if not self._yaml:
            self._yaml = make_yaml(self.vault)
        return self._yaml

    def __getstate__(self):
        state = self.__dict__.copy()
        # workarounds for 2.7:  Can't pickle <type 'instancemethod'>
        state["_yaml"] = None
        state["loadHook"] = None
        return state

    def validate(self, config):
        if isinstance(self.schema, str):
            # assume its a file path
            path = self.schema
            with open(path) as fp:
                self.schema = json.load(fp)
        else:
            path = None
        baseUri = None
        if path:
            baseUri = urljoin("file:", urllib.request.pathname2url(path))
        return find_schema_errors(config, self.schema, baseUri)

    def search_includes(
        self,
        key: Optional[str] = None,
        pathPrefix: Optional[str] = None,
        directive: Optional[str] = None,
    ) -> Union[Tuple[None, None], Tuple[str, dict]]:
        for k in self._cachedDocIncludes:
            path, template, extra = self._cachedDocIncludes[k]
            candidate = True
            if directive is not None:
                candidate = extra.lstrip("-") == directive.lstrip("-")
            if pathPrefix is not None:
                candidate = path.startswith(pathPrefix)
            if candidate and key is not None:
                candidate = key in template
            if candidate:
                return k, template
        return None, None

    def save_include(self, key):
        path, template, directive = self._cachedDocIncludes[key]
        if self.readonly:
            raise UnfurlError(
                f'Can not save include at "{path}", config "{self.path}" is readonly'
            )
        output = io.StringIO()
        try:
            self.yaml.dump(template, output)
        except Exception:
            raise UnfurlError(f"Error saving include {path}", True)
        with open(path, "w") as f:
            f.write(output.getvalue())

    def load_include(
        self,
        templatePath,
        warnWhenNotFound=False,
        expanded=None,
        check_or_path=False,
        directive="",
    ):
        if check_or_path and isinstance(check_or_path, bool):
            # called by merge.has_template
            if self.loadHook:
                return self.loadHook(
                    self,
                    templatePath,
                    self.baseDirs[-1],
                    warnWhenNotFound,
                    expanded,
                    LoadIncludeAction(directive, check_include=True),
                )
            else:
                return True

        if templatePath == self.baseDirs[-1]:  # pop hack
            self.baseDirs.pop()
            return

        if isinstance(templatePath, dict):
            value = templatePath.get("merge")
            if "file" not in templatePath:
                raise UnfurlError(
                    f"`file` key missing from document include: {templatePath}"
                )
            key = templatePath["file"]
            when = templatePath.get("when")
            if when and not os.getenv(when):
                return value, None, ""
        else:
            value = None
            key = templatePath

        if not isinstance(key, str):
            raise UnfurlError(f"Invalid include: {key}")

        if self.loadHook:
            # give loadHook a chance to transform the key
            key = self.loadHook(
                self,
                templatePath,
                self.baseDirs[-1],
                warnWhenNotFound,
                expanded,
                LoadIncludeAction(directive, get_key=key),
            )
            if not key:
                return value, None, ""

        if key in self._cachedDocIncludes:
            path, _template, directive = self._cachedDocIncludes[key]
            baseDir = os.path.dirname(path)
            self.baseDirs.append(baseDir)
            return value, _template, baseDir

        baseDir = self.baseDirs[-1]
        try:
            if self.loadHook:
                path, template = self.loadHook(
                    self, templatePath, baseDir, warnWhenNotFound, expanded, None
                )
            else:
                path, template = self.load_yaml(key, baseDir, warnWhenNotFound)
        except Exception:
            msg = f"unable to load document include: {templatePath} (base: {baseDir})"
            raise UnfurlError(msg, True)
        if template is None:
            return value, template, baseDir

        if path.startswith("http"):  # its an url, don't change baseDir
            newBaseDir = baseDir
        else:
            newBaseDir = os.path.dirname(path)
        if isinstance(template, dict):
            template.base_dir = newBaseDir  # type: ignore
        _cache_anchors(self.config._anchorCache, template)  # type: ignore
        self.baseDirs.append(newBaseDir)

        self._cachedDocIncludes[key] = (path, template, directive)
        return value, template, newBaseDir
