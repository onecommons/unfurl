# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
import os.path
import sys
import codecs
import json
import functools
from typing import Union
import six
from six.moves import urllib

try:
    from urlparse import urljoin  # Python2
except ImportError:
    from urllib.parse import urljoin  # Python3
pathname2url = urllib.request.pathname2url
from jsonschema import RefResolver

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.representer import RepresenterError, SafeRepresenter
from ruamel.yaml.constructor import ConstructorError

from .util import (
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
from .repo import is_url_or_git_path
from toscaparser.common.exception import URLException, ExceptionCollector
from toscaparser.utils.gettextutils import _
import toscaparser.imports
from toscaparser.repositories import Repository

from ansible.parsing.vault import VaultLib, VaultSecret
from ansible.utils.unsafe_proxy import AnsibleUnsafeText, AnsibleUnsafeBytes

import logging

logger = logging.getLogger("unfurl")

CLEARTEXT_VAULT = VaultLib(secrets=[("cleartext", b"")])


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
    cleartext = to_text(constructor.vault.decrypt(value))
    return sensitive_str(cleartext)


def construct_vaultjson(constructor, node):
    value = _construct_vault(constructor, node, "!vault-json")
    cleartext = to_text(constructor.vault.decrypt(value))
    return wrap_sensitive_value(json.loads(cleartext))


def construct_vaultbinary(constructor, node):
    value = _construct_vault(constructor, node, "!vault-binary")
    cleartext = constructor.vault.decrypt(value)
    return sensitive_bytes(cleartext)


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


def make_vault_lib(passwordBytes, vaultId="default"):
    if passwordBytes:
        if isinstance(passwordBytes, six.string_types):
            passwordBytes = to_bytes(passwordBytes)
        vault_secrets = [(vaultId, VaultSecret(passwordBytes))]
        return VaultLib(secrets=vault_secrets)
    return None


def make_vault_lib_ex(secrets: [str, Union[str, bytes]]):
    vault_secrets = []
    for vaultId, passwordBytes in secrets:
        if isinstance(passwordBytes, six.string_types):
            passwordBytes = to_bytes(passwordBytes)
        vault_secrets.append((vaultId, VaultSecret(passwordBytes)))
    return VaultLib(secrets=vault_secrets)


_refResolver = RefResolver("", None)


class ImportResolver(toscaparser.imports.ImportResolver):
    def __init__(self, manifest, ignoreFileNotFound=False, expand=False):
        self.manifest = manifest
        self.ignoreFileNotFound = ignoreFileNotFound
        self.loader = manifest.loader
        self.expand = expand

    def __getstate__(self):
        state = self.__dict__.copy()
        state["loader"] = None
        return state

    def get_repository(self, name, tpl):
        if isinstance(tpl, dict) and "url" in tpl:
            url = tpl["url"]
            if (
                "://" not in url and "@" in url
            ):  # scp style used by git: user@server:project.git
                # convert to ssh://user@server/project.git
                url = "ssh://" + url.replace(":", "/", 1)
            tpl["url"] = url
        return Repository(name, tpl)

    def get_url(self, importLoader, repository_name, file_name, isFile=None):
        # returns url or path, isFile, fragment
        importLoader.stream = None
        if repository_name:
            path = self.get_repository_url(importLoader, repository_name)
            file_name, sep, fragment = file_name.partition("#")
            if path.startswith("file:"):
                path = path[5:]
                if path.startswith("//"):
                    path = path[2:]
                isFile = True
        else:
            url_info = super().get_url(importLoader, repository_name, file_name, isFile)
            if not url_info:
                return url_info
            file_name = ""
            path, isFile, fragment = url_info

        if not isFile or is_url_or_git_path(path):
            # only support urls to git repos for now
            repo_view = self.manifest.repositories.get(repository_name)
            if repo_view and repo_view.repo:
                return os.path.join(repo_view.working_dir, file_name), True, fragment

            repo, filePath, revision, bare = self.manifest.find_repo_from_git_url(
                path, isFile, importLoader
            )
            if not repo:
                raise UnfurlError("Could not resolve git URL: " + path)
            isFile = True
            if bare:
                # XXX support empty filePath or when filePath is a directory -- need to checkout the tree
                if (
                    not filePath
                    or repo.repo.rev_parse(revision + ":" + filePath).type != "blob"
                ):
                    raise UnfurlError(
                        f"can't retrieve local repository for '{path}' with revision '{revision}'"
                    )
                importLoader.stream = six.StringIO(repo.show(filePath, revision))
                path = os.path.join(filePath, file_name or "")
            else:
                path = os.path.join(repo.working_dir, filePath, file_name or "").rstrip(
                    "/"
                )
                repository = None
                if repository_name:
                    repository = Repository(
                        repository_name, importLoader.repositories[repository_name]
                    )
                    self.manifest.add_repository(repo, repository, filePath)
        elif file_name:
            path = os.path.join(path, file_name)
        return path, isFile, fragment

    def load_yaml(self, importLoader, path, isFile=True, fragment=None):
        try:
            logger.debug(
                "attempting to load YAML %s: %s", "file" if isFile else "url", path
            )
            originalPath = path
            f = importLoader.stream

            if not f:
                try:
                    if isFile:
                        ignoreFileNotFound = self.ignoreFileNotFound
                        if ignoreFileNotFound and not os.path.isfile(path):
                            return None

                        if self.loader:
                            # show == True if file was decrypted
                            contents, show = self.loader._get_file_contents(path)
                            f = six.StringIO(codecs.decode(contents))
                        else:
                            f = codecs.open(path, encoding="utf-8", errors="strict")
                    else:
                        f = urllib.request.urlopen(path)
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
                        ) % {"path": path, "code": e.code}
                        ExceptionCollector.appendException(URLException(what=msg))
                        return
                    else:
                        raise
            with f:
                doc = yaml.load(f.read())
                if isinstance(doc, CommentedMap):
                    if self.expand:
                        includes, doc = expand_doc(
                            doc, cls=make_map_with_base(doc, get_base_dir(path))
                        )
                        doc.includes = includes
                    doc.path = path

            if fragment:
                return _refResolver.resolve_fragment(doc, fragment)
            else:
                return doc
        except:
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
    ):
        try:
            self._yaml = None
            self.vault = vault
            self.path = None
            self.schema = schema
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

            if isinstance(config, six.string_types):
                if self.path:
                    # set name on a StringIO so parsing error messages include the path
                    config = six.StringIO(config)
                    config.name = self.path
                self.config = self.yaml.load(config)
            elif isinstance(config, dict):
                self.config = CommentedMap(config.items())
            else:
                self.config = config
            if not isinstance(self.config, CommentedMap):
                raise UnfurlValidationError(
                    f'invalid YAML document with contents: "{self.config}"'
                )

            find_anchor(self.config, None)  # create _anchorCache
            self._cachedDocIncludes = {}
            # schema should include defaults but can't validate because it doesn't understand includes
            # but should work most of time
            self.config.loadTemplate = self.load_include
            self.loadHook = loadHook

            self.baseDirs = [self.get_base_dir()]
            self.includes, expandedConfig = expand_doc(
                self.config, cls=make_map_with_base(self.config, self.baseDirs[0])
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
        except Exception as e:
            if self.path:
                msg = f"Unable to load yaml config at {self.path}"
            else:
                msg = "Unable to parse yaml config"
            raise UnfurlError(msg, True)

    def load_yaml(self, path, baseDir=None, warnWhenNotFound=False):
        path = os.path.abspath(os.path.join(baseDir or self.get_base_dir(), path))
        if warnWhenNotFound and not os.path.isfile(path):
            return path, None
        logger.debug("attempting to load YAML file: %s", path)
        with open(path, "r") as f:
            config = self.yaml.load(f)
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
        except:
            raise UnfurlError(f"Error saving {self.path}", True)

    def save(self):
        output = six.StringIO()
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
        if isinstance(self.schema, six.string_types):
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
        output = six.StringIO()
        try:
            self.yaml.dump(template, output)
        except:
            raise UnfurlError(f"Error saving include {path}", True)
        with open(path, "w") as f:
            f.write(output.getvalue())

    def load_include(
        self, templatePath, warnWhenNotFound=False, expanded=None, check=False
    ):
        if check:
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
        else:
            value = None
            key = templatePath

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
            newBaseDir = os.path.dirname(path)
            if template is None:
                if warnWhenNotFound:
                    logger.warning(
                        f"document include {templatePath} does not exist (base: {baseDir})"
                    )
                return value, template, newBaseDir
            elif isinstance(template, CommentedMap):
                template.base_dir = newBaseDir
            _cache_anchors(self.config._anchorCache, template)
        except:
            raise UnfurlError(
                f"unable to load document include: {templatePath} (base: {baseDir})",
                True,
            )
        self.baseDirs.append(newBaseDir)

        self._cachedDocIncludes[key] = [path, template]
        return value, template, newBaseDir
