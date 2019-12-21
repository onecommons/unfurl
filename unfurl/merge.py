import itertools
import re
from collections import Mapping, MutableSequence

from ruamel.yaml.comments import CommentedMap, CommentedBase

from .util import UnfurlError


def makeMapWithBase(doc, baseDir):
    def factory(*args, **kws):
        map = CommentedMap(*args, **kws)
        map.baseDir = baseDir
        if hasattr(doc, "loadTemplate"):
            map.loadTemplate = doc.loadTemplate
        if hasattr(doc, "_anchorCache"):
            map._anchorCache = doc._anchorCache
        map.mapCtor = makeMapWithBase(doc, baseDir)
        return map

    return factory


# XXX?? because json keys are strings allow number keys to merge with lists
# other values besides delete not supported because current code can leave those keys in final result
mergeStrategyKey = "+%"  # values: delete

# b is the merge patch, a is original dict
def mergeDicts(b, a, cls=dict):
    """
  Returns a new dict (or cls) that recursively merges b into a.
  b is base, a overrides.

  Similar to https://yaml.org/type/merge.html but does a recursive merge
  """
    cp = cls()
    skip = []
    for key, val in a.items():
        if key == mergeStrategyKey:
            continue
        if key != mergeStrategyKey and isinstance(val, Mapping):
            strategy = val.get(mergeStrategyKey)
            if key in b:
                bval = b[key]
                if isinstance(bval, Mapping):
                    # merge strategy of a overrides b
                    if not strategy:
                        strategy = bval.get(mergeStrategyKey) or "merge"
                        if strategy == "merge":
                            cls = getattr(bval, "mapCtor", cls)
                            cp[key] = mergeDicts(bval, val, cls)
                            continue
                    if strategy == "error":
                        raise UnfurlError(
                            "merging %s is not allowed, +%: error was set" % key
                        )
                # otherwise we ignore bval because key is already in a
            if strategy == "delete":
                skip.append(key)
                continue
        # XXX merge lists
        # elif isinstance(val, list) and key in b:
        #   bval = b[key]
        #   if isinstance(bval, list):
        #     if appendlists == 'all' or key in appendlists:
        #       cp[key] = bval + val
        #       continue
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


def isIncludeKey(key):
    return key and key.startswith("%include")


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
    if isIncludeKey(key):
        value, template, baseDir = doc.loadTemplate(value, key.endswith("?"))
        if template is None:  # include wasn't not found and key ends with "?"
            return doc
        cls = makeMapWithBase(doc, baseDir)
    else:
        _jsonPointerValidate(key)
        for segment in key.split("/"):
            # XXX raise error if ../ sequence is not at the start of key
            if segment:
                if not segment.strip("."):  # segment is one or more '.'
                    stop = None
                    if len(segment) - 1:
                        stop = (len(segment) - 1) * -1
                    if templatePath is None:
                        templatePath = list(path[:stop])
                    else:
                        templatePath = templatePath[:stop]
                    template = lookupPath(doc, templatePath, cls)
                    continue
                elif segment[0] == "*":  # anchor reference
                    template = findAnchor(doc, segment[1:])
                    if template is None:
                        raise UnfurlError("could not find anchor '%s'" % segment[1:])
                    continue

            segment = _jsonPointerUnescape(segment)
            # XXX this check should allow array look up (fix hasTemplate too)
            if not isinstance(template, Mapping) or segment not in template:
                raise UnfurlError('can not find "%s" in document' % key)
            if templatePath is not None:
                templatePath.append(segment)
            template = template[segment]

        if templatePath is None:
            templatePath = [_jsonPointerUnescape(part) for part in key.split("/")]

    try:
        if value != "raw" and isinstance(
            template, Mapping
        ):  # raw means no further processing
            # if the include path starts with the path to the template
            # throw recursion error
            if not isIncludeKey(key):
                prefix = list(
                    itertools.takewhile(lambda x: x[0] == x[1], zip(path, templatePath))
                )
                if len(prefix) == len(templatePath):
                    raise UnfurlError(
                        'recursive include "%s" in "%s"' % (templatePath, path)
                    )
            includes = CommentedMap()
            template = expandDict(doc, path, includes, template, cls=cls)
    finally:
        if isIncludeKey(key):
            doc.loadTemplate(baseDir)  # pop baseDir
    return template


def hasTemplate(doc, key, path, cls):
    if isIncludeKey(key):
        return hasattr(doc, "loadTemplate")

    _jsonPointerValidate(key)
    template = doc
    for segment in key.split("/"):
        if not isinstance(template, Mapping):
            raise UnfurlError("included templates changed")
        if segment:
            if not segment.strip("."):
                stop = None
                if len(segment) - 1:
                    stop = (len(segment) - 1) * -1
                path = path[:stop]
                template = lookupPath(doc, path, cls)
                continue
            elif segment[0] == "*":  # anchor reference
                template = findAnchor(doc, segment[1:])
                if template is None:
                    return False
                continue
        if segment not in template:
            return False
        template = template[_jsonPointerUnescape(segment)]
    return True


class _MissingInclude(object):
    def __init__(self, key, value):
        self.key = key
        self.value = value

    def __repr__(self):
        return self.key


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
        if key.startswith("+"):
            if key == mergeStrategyKey:
                cp[key] = value
                continue
            foundTemplate = hasTemplate(doc, key[1:], path, cls)
            if not foundTemplate:
                includes.setdefault(path, []).append(_MissingInclude(key[1:], value))
                cp[key] = value
                continue
            includes.setdefault(path, []).append((key, value))
            template = getTemplate(doc, key[1:], value, path, cls)
            if isinstance(template, Mapping):
                templates.append(template)
            else:
                if len(current) > 1:  # XXX include merge directive keys in count
                    raise UnfurlError("can not merge non-map value %s" % template)
                else:
                    return template  # current dict is replaced with a value
        elif key.startswith("q+"):
            cp[key[2:]] = value
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
        if not isinstance(template, Mapping) or segment not in template:
            return None
        template = template[segment]
    return template


def replacePath(doc, key, value, cls=dict):
    path = key[:-1]
    last = key[-1]
    ref = lookupPath(doc, path, cls)
    ref[last] = value


def addTemplate(changedDoc, path, template):
    current = changedDoc
    key = path.split("/")
    path = key[:-1]
    last = key[-1]
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
            if isIncludeKey(includeKey[1:]):
                ref = None
                continue
            stillHasTemplate = hasTemplate(changedDoc, includeKey[1:], key, cls)
            if stillHasTemplate:
                template = getTemplate(
                    changedDoc, includeKey[1:], includeValue, key, cls
                )
            else:
                if hasTemplate(originalDoc, includeKey[1:], key, cls):
                    template = getTemplate(
                        originalDoc, includeKey[1:], includeValue, key, cls
                    )
                else:
                    template = getTemplate(
                        expandedOriginalDoc, includeKey[1:], includeValue, key, cls
                    )

            if not isinstance(ref, Mapping):
                # XXX3 if isinstance(ref, list) lists not yet implemented
                if ref == template:
                    # ref still resolves to the template's value so replace it with the include
                    replacePath(changedDoc, key, {includeKey: includeValue}, cls)
                # ref isn't a map anymore so can't include a template
                break

            if not isinstance(template, Mapping):
                # ref no longer includes that template or we don't want to save it
                continue
            else:
                mergedIncludes = mergeDicts(mergedIncludes, template, cls)
                ref[includeKey] = includeValue

            if not stillHasTemplate:
                if includeValue != "raw":
                    if hasTemplate(originalDoc, includeKey[1:], key, cls):
                        template = getTemplate(
                            originalDoc, includeKey[1:], "raw", key, cls
                        )
                    else:
                        template = getTemplate(
                            expandedOriginalDoc, includeKey[1:], "raw", key, cls
                        )
                addTemplate(changedDoc, includeKey[1:], template)

        if isinstance(ref, Mapping):
            diff = diffDicts(mergedIncludes, ref, cls)
            replacePath(changedDoc, key, diff, cls)
