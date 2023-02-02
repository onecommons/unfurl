# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
import io
import os.path
import sys
import codecs
import json
import os
from typing import Optional, TextIO, Union, Tuple, List, cast, TYPE_CHECKING, Dict
import urllib
from urllib.parse import urljoin, urlsplit
import ssl
import certifi

if TYPE_CHECKING:
    from .manifest import Manifest

pathname2url = urllib.request.pathname2url
from jsonschema import RefResolver

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.representer import RepresenterError, SafeRepresenter
from ruamel.yaml.constructor import ConstructorError

from .util import (
    filter_env,
    to_bytes,
    to_text,
    sensitive_str,
    sensitive_bytes,
    sensitive_dict,
    sensitive_list,
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
from .repo import RepoView, GitRepo, is_url_or_git_path, split_git_url
from .packages import resolve_package
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
    yaml_perf += (perf_counter() - start_time)
    return doc


def _use_clear_text(vault):
    clear_id = CLEARTEXT_VAULT.secrets[0][0]
    return vault.secrets and all(s[0] == clear_id for s in vault.secrets)


def represent_undefined(self, data):
    raise RepresenterError(f"cannot represent an object: <{data}> of type {type(data)}")


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


def make_vault_lib(passwordBytes: Union[str, bytes], vaultId="default") -> Optional[VaultLib]:
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

GetURLType = Optional[Tuple[str, bool, Optional[str]]]


class ImportResolver(toscaparser.imports.ImportResolver):
    def __init__(self, manifest: "Manifest", ignoreFileNotFound=False, expand=False, config=None):
        self.manifest = manifest
        self.readonly = bool(manifest.localEnv and manifest.localEnv.readonly)
        self.ignoreFileNotFound = ignoreFileNotFound
        self.yamlloader = manifest.loader
        self.expand = expand
        self.config = config or {}
        self.repo_refs: Dict[str, GitRepo] = {}

    def __getstate__(self):
        state = self.__dict__.copy()
        state["yamlloader"] = None
        return state

    def get_repository(self, name: str, tpl: dict) -> Repository:
        # don't create another Repository instance
        if name in self.manifest.repositories:
            return self.manifest.repositories[name].repository

        if isinstance(tpl, dict) and "url" in tpl:
            url = tpl["url"]
            if (
                "://" not in url and "@" in url
            ):  # scp style used by git: user@server:project.git
                # convert to ssh://user@server/project.git
                url = "ssh://" + url.replace(":", "/", 1)
            tpl["url"] = url

        if tpl.get('credential'):
            credential = tpl['credential']
            # support expressions to resolve credential secrets
            if self.manifest.rootResource:
                from .eval import map_value
                tpl['credential'] = map_value(credential, self.manifest.rootResource)
            elif self.manifest.localEnv:
                # we're including or importing before we finished initializing
                context = self.manifest.localEnv.get_context(self.config.get("environment"))
                tpl['credential'] = self.manifest.localEnv.map_value(credential, context.get("variables"))

        return Repository(name, tpl)

    def _get_bare_path(self, path, repo, filePath, revision, file_name, importsLoader):
        # # XXX support empty filePath or when filePath is a directory -- need to checkout the tree
        if not filePath:
            raise UnfurlError(
                f"local repository for '{path}' can not checkout revision '{revision}'"
            )
        elif repo.repo.rev_parse(revision + ":" + filePath).type != "blob":
            raise UnfurlError(
                f"can't retrieve '{filePath}' with revision '{revision}' from local repository for '{path}'"
            )
        url = "git-ref:" + repo.url
        self.repo_refs[url] = repo
        return f"{url}#{revision}:{filePath}"

    def _get_repository_path(self, importsLoader: toscaparser.imports.ImportsLoader,
                             repository_name: str) -> Tuple[str, bool, Optional["RepoView"]]:
        path = cast(str, self.get_repository_url(importsLoader, repository_name))
        if path.startswith("file:"):
            path = path[5:]
            if path.startswith("//"):
                path = path[2:]
            return path, True, None
        else:
            repoview = self.manifest.repositories.get(repository_name)
            if not repoview:
                repository = self.get_repository(repository_name, importsLoader.repositories[repository_name])
                repoview = self.manifest.add_repository(None, repository, "")
            assert repoview
            if repoview.package is None:
                # need to resolve if its a package
                # if repoview.repository references a package, set the repository's url
                # and register this reference with the package
                # might raise error if version conflict
                resolve_package(repoview, self.manifest.packages, self.manifest.package_specs)
            return repoview.url, False, repoview

    def get_url(self, importsLoader: toscaparser.imports.ImportsLoader,
                repository_name: Optional[str], file_name: str, isFile: Optional[bool] = None) -> GetURLType:
        # called by ImportsLoader when including or importing a file
        # or resolving path to an artifact in a repository (see ToscaSpec.get_repository_path)
        # returns url or path, isFile, fragment
        fragment: Optional[str] = None
        repo_view = None
        if repository_name:
            file_name, sep, fragment = file_name.partition("#")
            path, isFile, repo_view = self._get_repository_path(importsLoader, repository_name)
            # if not isFile path will be git url possibly including revision in fragment
            if repo_view and repo_view.repo:
                return os.path.join(repo_view.working_dir, file_name), True, fragment
        else:
            url_info = cast(GetURLType, super().get_url(importsLoader, None, file_name, isFile))
            if not url_info:
                return url_info
            path, isFile, fragment = url_info
            file_name = ""  # was included in path

        if not isFile or is_url_or_git_path(path):
            # only support urls to git repos for now
            # this may clone a remote git repo
            repo, filePath, revision, bare = self.manifest.find_repo_from_git_url(
                path, isFile, importsLoader
            )
            if not repo:
                raise UnfurlError("Could not resolve git URL: " + path)
            if bare:
                path = self._get_bare_path(path, repo, filePath, revision, file_name, importsLoader)
                return path, False, fragment
            else:
                if repo_view:
                    assert not repo_view.repo
                    repo_view.set_repo_and_path(repo, filePath)
                path = os.path.join(repo.working_dir, filePath, file_name or "").rstrip("/")
        elif file_name:
            path = os.path.join(path, file_name)

        # always a local file path at this point
        return path, True, fragment

    def load_yaml(self, importsLoader, path, isFile=True, fragment=None):
        try:
            logger.trace(
                "attempting to load YAML %s: %s", "file" if isFile else "url", path
            )
            originalPath = path

            if path.startswith("git-ref:"):
                url, filePath, revision = split_git_url(path)
                f: TextIO = io.StringIO(self.repo_refs[url].show(filePath, revision))
            else:
                try:
                    if isFile:
                        ignoreFileNotFound = self.ignoreFileNotFound
                        if ignoreFileNotFound and not os.path.isfile(path):
                            return None

                        if self.yamlloader:
                            # show == True if file was decrypted
                            contents, show = self.yamlloader._get_file_contents(path)
                            f = io.StringIO(codecs.decode(contents))
                        else:
                            f = codecs.open(path, encoding="utf-8", errors="strict")
                    else:
                        f = urlopen(path)
                except urllib.error.URLError as e:
                    if hasattr(e, "reason"):
                        msg = _(
                            (
                                'Failed to reach server "%(path)s". Reason is: '
                                + "%(reason)s."
                            )
                        ) % {"path": path, "reason": e.reason}
                        ExceptionCollector.appendException(URLException(what=msg))
                        return
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
                        return
                    else:
                        raise
            with f:
                contents = f.read()
                doc = load_yaml(yaml, contents, path, self.readonly)
                yaml_dict = yaml_dict_type(self.readonly)
                if isinstance(doc, yaml_dict):
                    if self.expand:
                        includes, doc = expand_doc(
                            doc, cls=make_map_with_base(doc, get_base_dir(path), yaml_dict)
                        )
                        doc.includes = includes
                    doc.path = path
            if fragment and doc:
                return _refResolver.resolve_fragment(doc, fragment)
            else:
                return doc
        except Exception:
            if path != originalPath:
                msg = f'Could not load "{path}" (originally "{originalPath}")'
            else:
                msg = f'Could not load "{path}"'
            raise UnfurlError(msg, True)


class YamlConfig:
    def __init__(
        self,
        config=None,
        path=None,
        validate=True,
        schema=None,
        loadHook=None,
        vault=None,
        readonly=False
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

            find_anchor(self.config, None)  # create _anchorCache
            self._cachedDocIncludes = {}
            # schema should include defaults but can't validate because it doesn't understand includes
            # but should work most of time
            self.config.loadTemplate = self.load_include  # type: ignore
            self.loadHook = loadHook

            self.baseDirs = [self.get_base_dir()]
            yaml_dict = yaml_dict_type(self.readonly)
            self.includes, expandedConfig = expand_doc(
                self.config, cls=make_map_with_base(self.config, self.baseDirs[0], yaml_dict)
            )
            self.expanded = expandedConfig
            errors = schema and self.validate(expandedConfig)
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

    def load_yaml(self, path, baseDir=None, warnWhenNotFound=False):
        url = urlsplit(path)
        if url.scheme.startswith("http") and url.netloc:  # looks like an absolute url
            fragment = url.fragment
            logger.trace("attempting to load YAML url: %s", path)
            try:
                f = urlopen(path)
            except urllib.error.URLError:
                if warnWhenNotFound:
                    logger.warning(f"document include {path} could not be retreived")
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
            raise UnfurlError(
                f'Can not save "{self.path}", it is set to readonly'
            )
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
                    check,
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
                key,
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
                    self, templatePath, baseDir, warnWhenNotFound, expanded
                )
            else:
                path, template = self.load_yaml(key, baseDir, warnWhenNotFound)

            if template is None:
                return value, template, baseDir

            if path.startswith("http"):  # its an url, don't change baseDir
                newBaseDir = baseDir
            else:
                newBaseDir = os.path.dirname(path)
            if isinstance(template, dict):
                template.base_dir = newBaseDir  # type: ignore
            _cache_anchors(self.config._anchorCache, template)
        except Exception:
            raise UnfurlError(
                f"unable to load document include: {templatePath} (base: {baseDir})",
                True,
            )
        self.baseDirs.append(newBaseDir)

        self._cachedDocIncludes[key] = [path, template]
        return value, template, newBaseDir
