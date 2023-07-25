# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
import io
import os.path
import sys
import codecs
import json
import os
from typing import Any, Optional, TextIO, Union, Tuple, List, cast, TYPE_CHECKING, Dict
import urllib
import urllib.request
from urllib.parse import urljoin, urlsplit
import ssl
import certifi
from jsonschema import RefResolver
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.representer import RepresenterError, SafeRepresenter
from ruamel.yaml.constructor import ConstructorError

if TYPE_CHECKING:
    from .manifest import Manifest

pathname2url = urllib.request.pathname2url

from .util import (
    filter_env,
    to_bytes,
    to_text,
    sensitive_str,
    sensitive_bytes,
    sensitive_dict,
    sensitive_list,
    unique_name,
    wrap_sensitive_value,
    UnfurlError,
    UnfurlValidationError,
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
from .repo import Repo, RepoView, add_user_to_url, split_git_url, memoized_remote_tags
from .packages import UnfurlPackageUpdateNeeded, resolve_package
from .logs import getLogger
from toscaparser.common.exception import URLException, ExceptionCollector
from toscaparser.utils.gettextutils import _
import toscaparser.imports
from toscaparser.repositories import Repository

from ansible.parsing.vault import VaultLib, VaultSecret
from ansible.parsing.yaml.objects import AnsibleMapping
from ansible.parsing.yaml.loader import AnsibleLoader, AnsibleConstructor
from ansible.utils.unsafe_proxy import AnsibleUnsafeText, AnsibleUnsafeBytes
from time import perf_counter
from jinja2.runtime import DebugUndefined

logger = getLogger("unfurl")

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


class ImportResolver(toscaparser.imports.ImportResolver):
    # get_repository() is called by the tosca template
    # load_yaml() called by the ImportsLoader with the resolved path or url returned by resolve_file_reference()

    def __init__(
        self, manifest: "Manifest", ignoreFileNotFound=False, expand=False, config=None
    ):
        self.manifest = manifest
        self.readonly = bool(manifest.localEnv and manifest.localEnv.readonly)
        self.ignoreFileNotFound = ignoreFileNotFound
        self.yamlloader = manifest.loader
        self.expand = expand
        self.config = config or {}

    def __getstate__(self):
        state = self.__dict__.copy()
        state["yamlloader"] = None
        return state

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

    def get_repository(self, name: str, tpl: dict, unique=False) -> Repository:
        if not unique and name in self.manifest.repositories:
            # don't create another Repository instance
            return self.manifest.repositories[name].repository
        else:
            name = unique_name(name, list(self.manifest.repositories))

        if isinstance(tpl, dict) and "url" in tpl:
            url = tpl["url"]
            if (
                "://" not in url and "@" in url
            ):  # scp style used by git: user@server:project.git
                # convert to ssh://user@server/project.git
                url = "ssh://" + url.replace(":", "/", 1)
            tpl["url"] = url

        if tpl.get("credential") and not self.manifest.safe_mode:
            credential = tpl["credential"]
            # support expressions to resolve credential secrets
            if self.manifest.rootResource:
                from .eval import map_value

                tpl["credential"] = map_value(credential, self.manifest.rootResource)
            elif self.manifest.localEnv:
                # we're including or importing before we finished initializing
                context = self.manifest.localEnv.get_context(
                    self.config.get("environment")
                )
                tpl["credential"] = self.manifest.localEnv.map_value(
                    credential, context.get("variables")
                )

        return Repository(name, tpl)

    confine_user_paths = True

    def _has_path_escaped(self, path, repository_name=None, base=None):
        if not self.confine_user_paths:
            return False
        if repository_name:
            if os.path.normpath(path).startswith("..") or os.path.isabs(path):
                msg = f'Path not allowed outside of repository {repository_name}: "{path}"'
                ExceptionCollector.appendException(ImportError(msg))
                return True
            else:
                return False

        if base:
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
        commit = repo_view.package.lock_to_commit if repo_view.package else ""
        if not repo_view.repo:
            # calls LocalEnv.find_or_create_working_dir()
            repo, file_path, revision, bare = self.manifest.find_repo_from_git_url(
                repo_view.as_git_url(), base
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
        else:
            password = ""
            candidate_parts = None
        if password:
            url_parts = urlsplit(url)
            assert candidate_parts
            if (
                candidate_parts.hostname == url_parts.hostname
                and candidate_parts.port == url_parts.port
            ):
                # rewrite url to add credentials
                url = add_user_to_url(
                    url, candidate_parts.username, password
                )
        return memoized_remote_tags(url, pattern="*")

    def resolve_url(
        self,
        importsLoader: toscaparser.imports.ImportsLoader,
        base: str,
        file_name: str,
        repository_name: Optional[str],
    ) -> Tuple[Optional[str], Optional[ImportResolver_Context]]:
        # resolve to an url or absolute path along with context
        if repository_name:
            if self._has_path_escaped(file_name, repository_name):
                # can't be ".." or absolute path
                return None, None
            repo_view = self.manifest.repositories.get(repository_name)
            if not repo_view:
                repository = self.get_repository(
                    repository_name, importsLoader.repositories[repository_name]
                )
                repo_view = self.manifest.add_repository(None, repository, "")
        else:
            # if file_name is relative, base will be set (to the importsLoader's path)
            url = os.path.join(base, file_name)
            if not toscaparser.imports.is_url(url):
                # url is a local path
                repository_root = None  # default to checking if its in the project
                if importsLoader.repository_root:
                    if toscaparser.imports.is_url(importsLoader.repository_root):
                        # just make sure we didn't break out of the base
                        repository_root = base
                    else:
                        repository_root = importsLoader.repository_root
                if self._has_path_escaped(url, base=repository_root):
                    return None, None
                return url, (True, None, base, file_name)
            repo_view = self._find_repoview(url)
            assert repo_view

        assert repo_view
        if repo_view.package is None:
            # need to resolve if its a package
            # if repoview.repository references a package, set the repository's url
            # and register this reference with the package
            # might raise error if version conflict
            resolve_package(
                repo_view,
                self.manifest.packages,
                self.manifest.package_specs,
                self.get_remote_tags,
            )
        repo_view.add_file_ref(file_name)
        path = toscaparser.imports.normalize_path(repo_view.url)
        is_file = not toscaparser.imports.is_url(path)
        if is_file:
            # repository is a local path
            # so use the resolved base
            path = os.path.join(base, file_name)
            if self._has_path_escaped(path):
                return None, None
        return path, (is_file, repo_view, base, file_name)

    def resolve_to_local_path(
        self, base_dir, file_name, repository_name
    ) -> Tuple[Optional[str], Optional[str]]:
        # this should only be called during deploy time when an operation needs direct access to directories in the repository
        # (see ArtifactSpec.get_path_and_fragment)
        if repository_name:
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
        # ctx is set by self.resolve_url()
        is_file, repo_view, base, file_name = ctx
        if not is_file:
            # clone git repository if necessary
            assert repo_view  # urls must have a repo_view
            path = self._resolve_repo_to_path(repo_view, base, file_name)
            is_file = True
        return self._really_load_yaml(path, is_file, fragment)

    def _really_load_yaml(
        self,
        path: str,
        isFile: bool,
        fragment: Optional[str],
    ) -> Tuple[Any, bool]:
        originalPath = path
        try:
            logger.trace(
                "attempting to load YAML %s: %s", "file" if isFile else "url", path
            )
            f, cacheable = self._open(path, isFile)
            if f is None:
                return None, cacheable

            with f:
                contents = f.read()
                doc = load_yaml(yaml, contents, path, self.readonly)
                yaml_dict = yaml_dict_type(self.readonly)
                if isinstance(doc, yaml_dict):
                    if self.expand:
                        # XXX doc won't have a loadTemplate attribute set so document includes aren't support here
                        # to support relative includes in a file in a repository, the repository root would have to be passed to the load include hook
                        includes, doc = expand_doc(
                            doc,
                            cls=make_map_with_base(doc, get_base_dir(path), yaml_dict),
                        )
                        doc.includes = includes
                    doc.path = path
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
        if doc_key in self.manifest.cache:
            val, count = self.manifest.cache[doc_key]
            self.manifest.cache[doc_key] = (val, count + 1)
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
            doc, cacheable = self._really_load_yaml(path, True, fragment)
            if cacheable:
                self.set_cache((path, fragment), doc)
        else:
            cacheable = True
        return doc, cacheable


class LoadIncludeAction:
    def __init__(self, check_include=False, get_key=None):
        self.get_key = get_key
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
            self.lastModified = None
            if path:
                self.path = os.path.abspath(path)
                if os.path.isfile(self.path):
                    statinfo = os.stat(self.path)
                    self.lastModified = statinfo.st_mtime
                    with open(self.path, "r") as f:
                        config = f.read()
                # otherwise use default config
            else:
                self.path = None

            if isinstance(config, str):
                self.config = load_yaml(self.yaml, config, self.path, self.readonly)
            elif isinstance(config, dict):
                self.config = CommentedMap(config.items())
            else:
                self.config = config
            if not isinstance(self.config, dict):
                raise UnfurlValidationError(
                    f'invalid YAML document with contents: "{self.config}"'
                )

            # schema should include defaults but can't validate because it doesn't understand includes
            # but should work most of time
            self.config.loadTemplate = self.load_include  # type: ignore
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
                raise UnfurlValidationError(
                    "JSON Schema validation failed: " + message, errors
                )
            else:
                self.valid = not not errors
        except Exception:
            if self.path:
                msg = f"Unable to load yaml config at {self.path}"
            else:
                msg = "Unable to parse yaml config"
            raise UnfurlError(msg, True)

    def _expand(self) -> Tuple[dict, dict]:
        find_anchor(self.config, None)  # create _anchorCache
        self._cachedDocIncludes: Dict[str, Tuple[str, dict]] = {}
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
            if self.lastModified:
                statinfo = os.stat(self.path)
                if statinfo.st_mtime > self.lastModified:
                    logger.error(
                        'Not saving "%s", it was unexpectedly modified after it was loaded, %d is after last modified time %d',
                        self.path,
                        statinfo.st_mtime,
                        self.lastModified,
                    )
                    raise UnfurlError(
                        f'Not saving "{self.path}", it was unexpectedly modified after it was loaded'
                    )
            with open(self.path, "w") as f:
                f.write(output.getvalue())
            statinfo = os.stat(self.path)
            self.lastModified = statinfo.st_mtime
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
            baseUri = urljoin("file:", pathname2url(path))
        return find_schema_errors(config, self.schema, baseUri)

    def search_includes(self, key=None, pathPrefix=None):
        for k in self._cachedDocIncludes:
            path, template = self._cachedDocIncludes[k]
            candidate = True
            if pathPrefix is not None:
                candidate = path.startswith(pathPrefix)
            if candidate and key is not None:
                candidate = key in template
            if candidate:
                return k, template
        return None, None

    def save_include(self, key):
        path, template = self._cachedDocIncludes[key]
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
        self, templatePath, warnWhenNotFound=False, expanded=None, check=False
    ):
        if check and isinstance(check, bool):
            # called by merge.has_template
            if self.loadHook:
                return self.loadHook(
                    self,
                    templatePath,
                    self.baseDirs[-1],
                    warnWhenNotFound,
                    expanded,
                    LoadIncludeAction(check_include=True),
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

        if self.loadHook:
            # give loadHook change to transform key
            key = self.loadHook(
                self,
                templatePath,
                self.baseDirs[-1],
                warnWhenNotFound,
                expanded,
                LoadIncludeAction(get_key=key),
            )
            if not key:
                return value, None, ""

        if key in self._cachedDocIncludes:
            path, template = self._cachedDocIncludes[key]
            baseDir = os.path.dirname(path)
            self.baseDirs.append(baseDir)
            return value, template, baseDir

        try:
            baseDir = self.baseDirs[-1]
            if self.loadHook:
                path, template = self.loadHook(
                    self, templatePath, baseDir, warnWhenNotFound, expanded, None
                )
            else:
                path, template = self.load_yaml(key, baseDir, warnWhenNotFound)
        except Exception:
            msg = f"unable to load document include: {templatePath} (base: {baseDir})"
            if warnWhenNotFound:
                logger.warning(msg, exc_info=True)
                template = None  # type: ignore
            else:
                raise UnfurlError(
                    msg,
                    True,
                )
        if template is None:
            return value, template, baseDir

        if path.startswith("http"):  # its an url, don't change baseDir
            newBaseDir = baseDir
        else:
            newBaseDir = os.path.dirname(path)
        if isinstance(template, dict):
            template.base_dir = newBaseDir  # type: ignore
        _cache_anchors(self.config._anchorCache, template)
        self.baseDirs.append(newBaseDir)

        self._cachedDocIncludes[key] = (path, template)
        return value, template, newBaseDir
