import os.path
import sys
import codecs
import json
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
from ruamel.yaml.representer import RepresenterError

from .util import sensitive_str, UnfurlError, UnfurlValidationError, findSchemaErrors
from .merge import expandDoc, makeMapWithBase, findAnchor, _cacheAnchors

from toscaparser.common.exception import ExceptionCollector
from toscaparser.common.exception import URLException
from toscaparser.utils.gettextutils import _

import logging

logger = logging.getLogger("unfurl")
yaml = YAML()
# monkey patch for better error message
def represent_undefined(self, data):
    raise RepresenterError(
        "cannot represent an object: %s of type %s" % (data, type(data))
    )


yaml.representer.represent_undefined = represent_undefined
yaml.representer.add_representer(None, represent_undefined)


def represent_sensitive(dumper, data):
    return dumper.represent_scalar(u"tag:yaml.org,2002:str", "<<REDACTED>>")


yaml.representer.add_representer(sensitive_str, represent_sensitive)


def resolveIfInRepository(manifest, path, isFile=True, importLoader=None):
    # XXX urls and files outside of a repo should be saved and commited to the repo the importLoader is in
    # check if this path is into a git repo
    repo, filePath, revision, bare = manifest.findRepoFromGitUrl(
        path, isFile, importLoader, True
    )
    if repo:  # it's a git repo
        if bare:
            return filePath, six.StringIO(repo.show(filePath, revision))
        # find the project that the loading file is in
        # tracks which commit was used, returns a workingDir
        workingDir = repo.checkout(revision)
        path = os.path.join(workingDir, filePath)
        isFile = True
    else:
        # if it's a file path, check if in one of our repos
        if isFile:
            repo, filePath, revision, bare = manifest.findPathInRepos(
                path, importLoader, True
            )
            if repo:
                if bare:
                    return filePath, six.StringIO(repo.show(filePath, revision))
                else:
                    path = os.path.join(repo.workingDir, filePath)
    return path, None

_refResolver = RefResolver('', None)
def load_yaml(path, isFile=True, importLoader=None, fragment=None):
    try:
        logger.debug(
            "attempting to load YAML %s: %s", "file" if isFile else "url", path
        )
        originalPath = path
        f = None
        manifest = importLoader and getattr(importLoader.tpl, "manifest", None)
        if manifest:
            path, f = resolveIfInRepository(manifest, path, isFile, importLoader)

        if not f:
            try:
                if isFile:
                    ignoreFileNotFound = importLoader and getattr(
                        importLoader.tpl, "ignoreFileNotFound", None
                    )
                    if ignoreFileNotFound and not os.path.isfile(path):
                        return None
                    f = codecs.open(path, encoding="utf-8", errors="strict")
                else:
                    f = urllib.request.urlopen(path)
            except urllib.error.URLError as e:
                if hasattr(e, "reason"):
                    msg = _(
                        'Failed to reach server "%(path)s". Reason is: ' "%(reason)s."
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
            except Exception as e:
                raise
        with f:
            doc = yaml.load(f.read())
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


import toscaparser.imports

toscaparser.imports.YAML_LOADER = load_yaml


def loadYamlFromArtifact(basePath, context, artifact):
    # _load_import_template will invoke load_yaml above
    loader = toscaparser.imports.ImportsLoader(None, basePath, tpl=context)
    return loader._load_import_template(None, artifact.asImportSpec())


class YamlConfig(object):
    def __init__(
        self, config=None, path=None, validate=True, schema=None, loadHook=None
    ):
        try:
            self.path = None
            self.schema = schema
            if path:
                self.path = os.path.abspath(path)
                if os.path.isfile(self.path):
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
                self.config = yaml.load(config)
            elif isinstance(config, dict):
                self.config = CommentedMap(config.items())
            else:
                self.config = config
            if not isinstance(self.config, CommentedMap):
                raise UnfurlValidationError("invalid YAML document: %s" % self.config)

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
            # print('expanded')
            # yaml.dump(config, sys.stdout)
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
            config = yaml.load(f)
        return path, config

    def getBaseDir(self):
        if self.path:
            return os.path.dirname(self.path)
        else:
            return "."

    def dump(self, out=sys.stdout):
        yaml.dump(self.config, out)

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

    def loadInclude(self, templatePath, warnWhenNotFound=False):
        if templatePath == self.baseDirs[-1]:
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
