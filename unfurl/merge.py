# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
import itertools
import re
import six
from collections import Mapping, MutableSequence, namedtuple, Sequence

from ruamel.yaml.comments import CommentedMap, CommentedBase

from .util import UnfurlError


def _mapCtor(self):
    if hasattr(self, "baseDir"):
        return makeMapWithBase(self, self.baseDir)
    return CommentedMap


CommentedMap.mapCtor = property(_mapCtor)


def copy(src):
    cls = getattr(src, "mapCtor", src.__class__)
    if six.PY2 and cls is CommentedMap:
        return CommentedMap(src.items())
    return cls(src)


def makeMapWithBase(doc, baseDir):
    loadTemplate = getattr(doc, "loadTemplate", None)
    _anchorCache = getattr(doc, "_anchorCache", None)

    def factory(*args, **kws):
        if six.PY2 and args and isinstance(args[0], dict):
            map = CommentedMap(args[0].items(), **kws)
        else:
            map = CommentedMap(*args, **kws)
        map.baseDir = baseDir
        if loadTemplate:
            map.loadTemplate = loadTemplate
        if _anchorCache is not None:
            map._anchorCache = _anchorCache
        return map

    return factory


# XXX?? because json keys are strings allow number keys to merge with lists
# other values besides delete not supported because current code can leave those keys in final result
mergeStrategyKey = "+%"  # values: delete

# b is the merge patch, a is original dict
def mergeDicts(b, a, cls=None, replaceKeys=None, defaultStrategy="merge"):
    """
    Returns a new dict (or cls) that recursively merges b into a.
    b is base, a overrides.

    Similar to https://yaml.org/type/merge.html but does a recursive merge
    """
    cls = getattr(b, "mapCtor", cls or b.__class__)
    cp = cls()
    skip = []
    for key, val in a.items():
        if key == mergeStrategyKey:
            continue
        if replaceKeys and key in replaceKeys:
            childStrategy = "replace"
        else:
            childStrategy = "merge"
        if isinstance(val, Mapping):
            strategy = val.get(mergeStrategyKey)
            if key in b:
                bval = b[key]
                if isinstance(bval, Mapping):
                    # merge strategy of a overrides b
                    if not strategy:
                        strategy = bval.get(mergeStrategyKey) or defaultStrategy
                    if strategy == "merge":
                        if not val:  # empty map, treat as missing key
                            continue
                        cp[key] = mergeDicts(
                            bval,
                            val,
                            defaultStrategy=childStrategy,
                            replaceKeys=replaceKeys,
                        )
                        continue
                    if strategy == "error":
                        raise UnfurlError(
                            "merging %s is not allowed, +%%: error was set" % key
                        )
                # otherwise we ignore bval because key is already in a
            if strategy == "delete":
                skip.append(key)
                continue
        elif isinstance(val, MutableSequence) and key in b:
            bval = b[key]
            if isinstance(bval, MutableSequence):
                # XXX allow more strategies beyond append
                #     if appendlists == 'all' or key in appendlists:
                cp[key] = bval + [item for item in val if item not in bval]
                continue
        #     elif mergelists == 'all' or key in mergelists:
        #       newlist = []
        #       for ai, bi in zip(val, bval):
        #         if isinstance(ai, Mapping) and isinstance(bi, Mapping):
        #           newlist.append(mergeDicts(bi, ai, cls))
        #         elif a1 != deletemarker:
        #           newlist.append(a1)
        #       cp[key] == newlist
        #       continue

        # otherwise a replaces b
        cp[key] = val

    # add new keys
    for key, val in b.items():
        if key == mergeStrategyKey:
            continue
        if key not in cp and key not in skip:
            # note: val is shared not copied
            cp[key] = val
    return cp


def _cacheAnchors(_anchorCache, obj):
    anchor = obj.yaml_anchor()
    if anchor and anchor.value:
        _anchorCache[anchor.value] = obj
        # By default, anchors are only emitted when it detects a reference t
        # to an object previously seen, so force it to be emitted
        anchor.always_dump = True

    for value in obj.values() if isinstance(obj, dict) else obj:
        if isinstance(value, CommentedBase):
            _cacheAnchors(_anchorCache, value)


def findAnchor(doc, anchorName):
    if not isinstance(doc, CommentedMap):
        return None

    _anchorCache = getattr(doc, "_anchorCache", None)
    if _anchorCache is None:
        _anchorCache = {}
        # recursively find anchors
        _cacheAnchors(_anchorCache, doc)
        doc._anchorCache = _anchorCache

    return _anchorCache.get(anchorName)


def _jsonPointerUnescape(s):
    return s.replace("~1", "/").replace("~0", "~")


_RE_INVALID_JSONPOINTER_ESCAPE = re.compile("(~[^01]|~$)")


def _jsonPointerValidate(pointer):
    invalid_escape = _RE_INVALID_JSONPOINTER_ESCAPE.search(pointer)
    if invalid_escape:
        raise UnfurlError(
            "Found invalid escape {} in JSON pointer {}".format(
                invalid_escape.group(), pointer
            )
        )
    return None


def getTemplate(doc, key, value, path, cls):
    template = doc
    templatePath = None
    if key.include:
        value, template, baseDir = doc.loadTemplate(value, key.maybe)
        if template is None:  # include wasn't not found and key.maybe
            return None
        cls = makeMapWithBase(doc, baseDir)
        # fileKey = key._replace(include=None)
        # template = getTemplate(template, fileKey, "raw", (), cls)
    else:
        result = _findTemplate(doc, key, path, cls, not key.maybe)
        if result is None:
            return doc
        template, templatePath = result
        if templatePath is None:
            templatePath = key.pointer

    try:
        if value != "raw" and isinstance(
            template, Mapping
        ):  # raw means no further processing

            # if the include path starts with the path to the template
            # throw recursion error
            if not key.include and not key.anchor:
                prefix = list(
                    itertools.takewhile(lambda x: x[0] == x[1], zip(path, templatePath))
                )
                if len(prefix) == len(templatePath):
                    raise UnfurlError(
                        'recursive include "%s" in "%s" when including %s'
                        % (templatePath, path, key.key)
                    )
            includes = CommentedMap()
            template = expandDict(doc, path, includes, template, cls=cls)
    finally:
        if key.include:
            doc.loadTemplate(baseDir)  # pop baseDir
    return template


def _findTemplate(doc, key, path, cls, fail):
    template = doc
    templatePath = None
    if key.anchor:
        template = findAnchor(doc, key.anchor)
        if template is None:
            if not fail:
                return None
            else:
                raise UnfurlError("could not find anchor '%s'" % key.anchor)
        # XXX we don't know the path to the anchor so we can't support relative paths also
    elif key.relative:
        stop = None
        if key.relative - 1:
            stop = (key.relative - 1) * -1
        if templatePath is None:
            templatePath = list(path[:stop])
        else:
            templatePath = templatePath[:stop]
        template = lookupPath(doc, templatePath, cls)
        if template is None:
            if not fail:
                return None
            else:
                raise UnfurlError("could relative path '%s'" % ("." * key.relative))
    for index, segment in enumerate(key.pointer):
        if isinstance(template, Sequence):
            # Array indexes should be turned into integers
            try:
                segment = int(segment)
            except ValueError:
                pass
        try:
            template = template[segment]
        except (TypeError, LookupError):
            if not fail:
                return None
            raise UnfurlError(
                'can not find "%s" in document' % key.pointer[: index + 1].join("/")
            )

        if templatePath is not None:
            templatePath.append(segment)
    return template, templatePath


def hasTemplate(doc, key, path, cls):
    if key.include:
        return hasattr(doc, "loadTemplate")
    return _findTemplate(doc, key, path, cls, False) is not None


class _MissingInclude(object):
    def __init__(self, key, value):
        self.key = key
        self.value = value

    def __repr__(self):
        return self.key.key


RE_FIRST = re.compile(r"([?]?)(include\d*)?([*]\S+)?([.]+$)?")

MergeKey = namedtuple("MergeKey", "key, maybe, include, anchor, relative, pointer")


def parseMergeKey(key):
    """
    +[maybe]?[include]?[anchor]?[relative]?[jsonpointer]?

    [include] = "include"[number?]
    [anchor] = "*"[anchorname]
    relative = '.'+
    """
    original = key
    key = key[1:]
    _jsonPointerValidate(key)
    parts = [_jsonPointerUnescape(part) for part in key.split("/")]
    first = parts.pop(0)
    maybe, include, anchor, relative = RE_FIRST.match(first).groups()
    if anchor:
        # relative will be included in anchor
        relative = len(anchor.rstrip(".")) - len(anchor)
        anchor = anchor[1:]  # exclude '*''
    elif relative:
        relative = len(relative)
    else:
        relative = 0
    if not (maybe or include or anchor or relative):
        if first:  # first wasn't empty and didn't match
            return None
        elif not parts:
            return None  # first was empty and pointer portion was too
    return MergeKey(original, not not maybe, include, anchor, relative, tuple(parts))


def expandDict(doc, path, includes, current, cls=dict):
    """
    Return a copy of `doc` that expands include directives.
    Include directives look like "+path/to/value"
    When appearing as a key in a map it will merge the result with the current dictionary.
    When appearing in a list it will insert the result in the list;
    if result is also a list, each item will be inserted separately.
    (If you don't want that behavior just wrap include in another list, e.g "[+list1]")
    """
    cp = cls()
    # first merge any includes includes into cp
    templates = []
    assert isinstance(current, Mapping), current
    for (key, value) in current.items():
        if not isinstance(key, six.string_types):
            cp[key] = value
            continue
        if key.startswith("+"):
            if key == mergeStrategyKey:
                cp[key] = value
                continue
            mergeKey = parseMergeKey(key)
            if not mergeKey:
                cp[key] = value
                continue
            foundTemplate = hasTemplate(doc, mergeKey, path, cls)
            if not foundTemplate:
                includes.setdefault(path, []).append(_MissingInclude(mergeKey, value))
                cp[key] = value
                continue
            includes.setdefault(path, []).append((mergeKey, value))
            template = getTemplate(doc, mergeKey, value, path, cls)
            if isinstance(template, Mapping):
                templates.append(template)
            elif mergeKey.include and template is None:
                continue  # include path not found
            else:
                if len(current) > 1:  # XXX include merge directive keys in count
                    raise UnfurlError("can not merge non-map value %s" % template)
                else:
                    return template  # current dict is replaced with a value
        # elif key.startswith("q+"):
        #    cp[key[2:]] = value
        elif isinstance(value, Mapping):
            cp[key] = expandDict(doc, path + (key,), includes, value, cls)
        elif isinstance(value, list):
            cp[key] = list(expandList(doc, path + (key,), includes, value, cls))
        else:
            cp[key] = value

    if templates:
        accum = templates.pop(0)
        templates.append(cp)
        while templates:
            cls = getattr(templates[0], "mapCtor", cls)
            accum = mergeDicts(accum, templates.pop(0), cls)
        return accum
    else:
        return cp
    # e,g, mergeDicts(mergeDicts(a, b), cp)
    # return includes, reduce(lambda accum, next: mergeDicts(accum, next, cls), templates, {}), cp


def _findMissingIncludes(includes):
    for x in includes.values():
        for i in x:
            if isinstance(i, _MissingInclude):
                yield i


def _deleteDeletedKeys(expanded):
    for key, value in expanded.items():
        if isinstance(value, Mapping):
            if value.get(mergeStrategyKey) == "delete":
                del expanded[key]
            else:
                _deleteDeletedKeys(value)


def expandDoc(doc, current=None, cls=dict):
    includes = CommentedMap()
    if current is None:
        current = doc
    if not isinstance(doc, Mapping) or not isinstance(current, Mapping):
        raise UnfurlError("top level element %s is not a dict" % doc)
    expanded = expandDict(doc, (), includes, current, cls)
    if hasattr(doc, "_anchorCache"):
        expanded._anchorCache = doc._anchorCache
    last = 0
    while True:
        missing = list(_findMissingIncludes(includes))
        if len(missing) == 0:
            # remove any stray keys with delete merge directive
            _deleteDeletedKeys(expanded)
            return includes, expanded
        if len(missing) == last:  # no progress
            raise UnfurlError("missing includes: %s" % missing)
        last = len(missing)
        includes = CommentedMap()
        expanded = expandDict(expanded, (), includes, current, cls)
        if hasattr(doc, "_anchorCache"):
            expanded._anchorCache = doc._anchorCache


def expandList(doc, path, includes, value, cls=dict):
    for i, item in enumerate(value):
        if isinstance(item, Mapping):
            if item.get(mergeStrategyKey) == "delete":
                continue
            newitem = expandDict(doc, path + (i,), includes, item, cls)
            if isinstance(newitem, MutableSequence):
                for i in newitem:
                    yield i
            else:
                yield newitem
        else:
            yield item


def diffDicts(old, new, cls=dict):
    """
    return a dict where old + diff = new
    """
    diff = cls()
    # start with old to preserve original order
    for key, val in old.items():
        if key in new:
            newval = new[key]
            if val != newval:
                if isinstance(val, Mapping) and isinstance(newval, Mapping):
                    diff[key] = diffDicts(val, newval, cls)
                else:
                    diff[key] = newval
        else:
            diff[key] = {"+%": "delete"}

    for key in new:
        if key not in old:
            diff[key] = new[key]
    return diff


# XXX rename function, confusing name
def patchDict(old, new, cls=dict):
    """
    Transform old into new while preserving old as much as possible.
    """
    # start with old to preserve original order
    for key, val in list(old.items()):
        if key in new:
            newval = new[key]
            if val != newval:
                if isinstance(val, Mapping) and isinstance(newval, Mapping):
                    old[key] = patchDict(val, newval, cls)
                elif isinstance(val, MutableSequence) and isinstance(
                    newval, MutableSequence
                ):
                    # preserve old item in list if they are equal to the new item
                    old[key] = [
                        (val[val.index(item)] if item in val else item)
                        for item in newval
                    ]
                else:
                    old[key] = newval
        else:
            del old[key]

    for key in new:
        if key not in old:
            old[key] = new[key]

    return old


def intersectDict(old, new, cls=dict):
    """
    remove keys from old that don't match new
    """
    # start with old to preserve original order
    for key, val in list(old.items()):
        if key in new:
            newval = new[key]
            if val != newval:
                if isinstance(val, Mapping) and isinstance(newval, Mapping):
                    old[key] = intersectDict(val, newval, cls)
                else:
                    del old[key]
        else:
            del old[key]

    return old


def lookupPath(doc, path, cls=dict):
    template = doc
    for segment in path:
        if isinstance(template, Sequence):
            try:
                segment = int(segment)
            except ValueError:
                return None
        try:
            template = template[segment]
        except (TypeError, LookupError):
            return None
    return template


def replacePath(doc, key, value, cls=dict):
    path = key[:-1]
    last = key[-1]
    ref = lookupPath(doc, path, cls)
    ref[last] = value


def addTemplate(changedDoc, path, mergeKey, template, cls):
    # if includeKey.anchor: #???
    if mergeKey.relative:
        if mergeKey.relative > 1:
            path = path[: (mergeKey.relative - 1) * -1]
        current = lookupPath(changedDoc, path, cls)
    else:
        current = changedDoc

    assert mergeKey.pointer
    path = mergeKey.pointer[:-1]
    last = mergeKey.pointer[-1]
    for segment in path:
        current = current.setdefault(segment, {})
    current[last] = template


def restoreIncludes(includes, originalDoc, changedDoc, cls=dict):
    """
    Modifies changedDoc with to use the includes found in originalDoc
    """
    # if the path to the include still exists
    # resolve the include
    # if the include doesn't exist in the current doc, re-add it
    # create a diff between the current object and the merged includes
    expandedOriginalIncludes, expandedOriginalDoc = expandDoc(originalDoc, cls=cls)
    for key, value in includes.items():
        ref = lookupPath(changedDoc, key, cls)
        if ref is None:
            # inclusion point no longer exists
            continue

        mergedIncludes = {}
        for (includeKey, includeValue) in value:
            if includeKey.include:
                ref = None
                continue
            stillHasTemplate = hasTemplate(changedDoc, includeKey, key, cls)
            if stillHasTemplate:
                template = getTemplate(changedDoc, includeKey, includeValue, key, cls)
            else:
                if hasTemplate(originalDoc, includeKey, key, cls):
                    template = getTemplate(
                        originalDoc, includeKey, includeValue, key, cls
                    )
                else:
                    template = getTemplate(
                        expandedOriginalDoc, includeKey, includeValue, key, cls
                    )

            if not isinstance(ref, Mapping):
                # XXX3 if isinstance(ref, list) lists not yet implemented
                if ref == template:
                    # ref still resolves to the template's value so replace it with the include
                    replacePath(changedDoc, key, {includeKey.key: includeValue}, cls)
                # ref isn't a map anymore so can't include a template
                break

            if not isinstance(template, Mapping):
                # ref no longer includes that template or we don't want to save it
                continue
            else:
                mergedIncludes = mergeDicts(mergedIncludes, template, cls)
                ref[includeKey.key] = includeValue

            if not stillHasTemplate:
                if includeValue != "raw":
                    if hasTemplate(originalDoc, includeKey, key, cls):
                        template = getTemplate(originalDoc, includeKey, "raw", key, cls)
                    else:
                        template = getTemplate(
                            expandedOriginalDoc, includeKey, "raw", key, cls
                        )
                addTemplate(changedDoc, key, includeKey, template, cls)

        if isinstance(ref, Mapping):
            diff = diffDicts(mergedIncludes, ref, cls)
            replacePath(changedDoc, key, diff, cls)
