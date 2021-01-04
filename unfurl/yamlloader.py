# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
import os.path
import sys
import codecs
import json
import functools
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
    wrapSensitiveValue,
    UnfurlError,
    UnfurlValidationError,
    findSchemaErrors,
)
from .merge import (
    expandDoc,
    makeMapWithBase,
    findAnchor,
    _cacheAnchors,
    restoreIncludes,
)
from .repo import isURLorGitPath
from toscaparser.common.exception import URLException, ExceptionCollector
from toscaparser.utils.gettextutils import _
import toscaparser.imports
from toscaparser.repositories import Repository

from ansible.parsing.vault import VaultLib, VaultSecret
from ansible.utils.unsafe_proxy import AnsibleUnsafeText, AnsibleUnsafeBytes

import logging

logger = logging.getLogger("unfurl")


def represent_undefined(self, data):
    raise RepresenterError(
        "cannot represent an object: <%s> of type %s" % (data, type(data))
    )


def _represent_sensitive(dumper, data, tag):
    if dumper.vault.secrets:
        b_ciphertext = dumper.vault.encrypt(data)
        return dumper.represent_scalar(tag, b_ciphertext.decode(), style="|")
    else:
        return dumper.represent_scalar(
            u"tag:yaml.org,2002:str", sensitive_str.redacted_str
        )


def represent_sensitive_str(dumper, data):
    return _represent_sensitive(dumper, data, u"!vault")


def represent_sensitive_json(dumper, data):
    return _represent_sensitive(
        dumper, json.dumps(data, sort_keys=True), u"!vault-json"
    )


def represent_sensitive_bytes(dumper, data):
    return _represent_sensitive(dumper, data, u"!vault-binary")


def _construct_vault(constructor, node, tag):
    value = constructor.construct_scalar(node)
    if not constructor.vault.secrets:
        raise ConstructorError(
            context=None,
            context_mark=None,
            problem="found %s but no vault password provided" % tag,
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
    return wrapSensitiveValue(json.loads(cleartext))


def construct_vaultbinary(constructor, node):
    value = _construct_vault(constructor, node, "!vault-binary")
    cleartext = constructor.vault.decrypt(value)
    return sensitive_bytes(cleartext)


def makeYAML(vault=None):
    if not vault:
        vault = VaultLib(secrets=None)
    yaml = YAML()

    # monkey patch for better error message
    yaml.representer.represent_undefined = represent_undefined
    yaml.representer.add_representer(None, represent_undefined)

    yaml.constructor.vault = vault
    yaml.constructor.add_constructor(u"!vault", construct_vault)
    yaml.constructor.add_constructor(u"!vault-json", construct_vaultjson)
    yaml.constructor.add_constructor(u"!vault-binary", construct_vaultbinary)

    yaml.representer.vault = vault
    yaml.representer.add_representer(sensitive_str, represent_sensitive_str)
    yaml.representer.add_representer(sensitive_dict, represent_sensitive_json)
    yaml.representer.add_representer(sensitive_list, represent_sensitive_json)
    yaml.representer.add_representer(sensitive_bytes, represent_sensitive_bytes)

    if six.PY3:
        represent_unicode = SafeRepresenter.represent_str
        represent_binary = SafeRepresenter.represent_binary
    else:
        represent_unicode = SafeRepresenter.represent_unicode
        represent_binary = SafeRepresenter.represent_str

    yaml.representer.add_representer(AnsibleUnsafeText, represent_unicode)
    yaml.representer.add_representer(AnsibleUnsafeBytes, represent_binary)
    return yaml


yaml = makeYAML()


def makeVaultLib(passwordBytes, vaultId="default"):
    if passwordBytes:
        if isinstance(passwordBytes, six.string_types):
            passwordBytes = to_bytes(passwordBytes)
        vault_secrets = [(vaultId, VaultSecret(passwordBytes))]
        return VaultLib(secrets=vault_secrets)
    return None


_refResolver = RefResolver("", None)


class ImportResolver(toscaparser.imports.ImportResolver):
    def __init__(self, manifest, ignoreFileNotFound=False):
        self.manifest = manifest
        self.ignoreFileNotFound = ignoreFileNotFound
        self.loader = manifest.loader

    def get_url(self, importLoader, repository_name, file_name, isFile=None):
        # returns url or path, isFile, fragment
        importLoader.stream = None
        if repository_name:
            # don't include file_name, we need to keep it distinct from the repo path till the end
            url_info = super(ImportResolver, self).get_url(
                importLoader, repository_name, "", isFile
            )
        else:
            url_info = super(ImportResolver, self).get_url(
                importLoader, repository_name, file_name, isFile
            )
            file_name = ""
        if not url_info:
            return url_info

        path, isFile, fragment = url_info
        if not isFile or isURLorGitPath(path):  # only support urls to git repos for now
            repo, filePath, revision, bare = self.manifest.findRepoFromGitUrl(
                path, isFile, importLoader
            )
            if not repo:
                raise UnfurlError("Could not resolve git URL: " + path)
            isFile = True
            if bare:
                if not filePath:
                    raise UnfurlError(
                        "can't retrieve local repository for '%s' with revision '%s'"
                        % (path, revision)
                    )
                importLoader.stream = six.StringIO(repo.show(filePath, revision))
                path = os.path.join(filePath, file_name or "")
            else:
                path = os.path.join(repo.workingDir, filePath, file_name or "").rstrip(
                    "/"
                )
                repository = None
                if repository_name:
                    repository = Repository(
                        repository_name, importLoader.repositories[repository_name]
                    )
                    self.manifest.addRepository(repo, repository, filePath)
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
                            'Failed to reach server "%(path)s". Reason is: '
                            "%(reason)s."
                        ) % {"path": path, "reason": e.reason}
                        ExceptionCollector.appendException(URLException(what=msg))
                        return
                    elif hasattr(e, "code"):
                        msg = _(
                            'The server "%(path)s" couldn\'t fulfill the request. '
                            'Error code: "%(code)s".'
                        ) % {"path": path, "code": e.code}
                        ExceptionCollector.appendException(URLException(what=msg))
                        return
                    else:
                        raise
            with f:
                doc = yaml.load(f.read())
                if isinstance(doc, CommentedMap):
                    doc.path = path
            if fragment:
                return _refResolver.resolve_fragment(doc, fragment)
            else:
                return doc
        except:
            if path != originalPath:
                msg = 'Could not load "%s" (originally "%s")' % (path, originalPath)
            else:
                msg = 'Could not load "%s"' % path
            raise UnfurlError(msg, True)


class YamlConfig(object):
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
                    'invalid YAML document with contents: "%s"' % self.config
                )

            findAnchor(self.config, None)  # create _anchorCache
            self._cachedDocIncludes = {}
            # schema should include defaults but can't validate because it doesn't understand includes
            # but should work most of time
            self.config.loadTemplate = self.loadInclude
            self.loadHook = loadHook

            self.baseDirs = [self.getBaseDir()]
            self.includes, expandedConfig = expandDoc(
                self.config, cls=makeMapWithBase(self.config, self.baseDirs[0])
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
        except:
            if self.path:
                msg = "Unable to load yaml config at %s" % self.path
            else:
                msg = "Unable to parse yaml config"
            raise UnfurlError(msg, True)

    def loadYaml(self, path, baseDir=None, warnWhenNotFound=False):
        path = os.path.abspath(os.path.join(baseDir or self.getBaseDir(), path))
        if warnWhenNotFound and not os.path.isfile(path):
            return path, None
        with open(path, "r") as f:
            config = self.yaml.load(f)
        return path, config

    def getBaseDir(self):
        if self.path:
            return os.path.dirname(self.path)
        else:
            return "."

    def restoreIncludes(self, changed):
        restoreIncludes(self.includes, self.config, changed, cls=CommentedMap)

    def dump(self, out=sys.stdout):
        try:
            self.yaml.dump(self.config, out)
        except:
            raise UnfurlError("Error saving %s" % self.path, True)

    def save(self):
        output = six.StringIO()
        self.dump(output)
        if self.path:
            if self.lastModified:
                statinfo = os.stat(self.path)
                if statinfo.st_mtime > self.lastModified:
                    raise UnfurlError(
                        'Not saving "%s", it was unexpectedly modified after it was loaded'
                        % self.path
                    )
            with open(self.path, "w") as f:
                f.write(output.getvalue())
            statinfo = os.stat(self.path)
            self.lastModified = statinfo.st_mtime
        return output

    @property
    def yaml(self):
        if not self._yaml:
            self._yaml = makeYAML(self.vault)
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
        return findSchemaErrors(config, self.schema, baseUri)

    def searchIncludes(self, key=None, pathPrefix=None):
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

    def saveInclude(self, key):
        path, template = self._cachedDocIncludes[key]
        output = six.StringIO()
        try:
            self.yaml.dump(template, output)
        except:
            raise UnfurlError("Error saving include %s" % path, True)
        with open(path, "w") as f:
            f.write(output.getvalue())

    def loadInclude(self, templatePath, warnWhenNotFound=False):
        if templatePath == self.baseDirs[-1]:  # pop hack
            self.baseDirs.pop()
            return

        if isinstance(templatePath, dict):
            value = templatePath.get("merge")
            if "file" not in templatePath:
                raise UnfurlError(
                    "`file` key missing from document include: %s" % templatePath
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
                    self, templatePath, baseDir, warnWhenNotFound
                )
            else:
                path, template = self.loadYaml(key, baseDir, warnWhenNotFound)
            newBaseDir = os.path.dirname(path)
            if template is None:
                if warnWhenNotFound:
                    logger.warning(
                        "document include %s does not exist (base: %s)"
                        % (templatePath, baseDir)
                    )
                return value, template, newBaseDir
            elif isinstance(template, CommentedMap):
                template.baseDir = newBaseDir
            _cacheAnchors(self.config._anchorCache, template)
        except:
            raise UnfurlError(
                "unable to load document include: %s (base: %s)"
                % (templatePath, baseDir),
                True,
                True,
            )
        self.baseDirs.append(newBaseDir)

        self._cachedDocIncludes[key] = [path, template]
        return value, template, newBaseDir
