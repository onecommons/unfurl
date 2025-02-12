# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
import itertools
import re
from typing import Any, Callable, Dict, List, MutableMapping, Optional, Tuple, Union
from collections import namedtuple
from collections.abc import Mapping, MutableSequence, Sequence

from ruamel.yaml.comments import CommentedMap, CommentedBase
from ansible.parsing.yaml.objects import AnsibleMapping

from .util import UnfurlError


def __as_dict_repr__(self):
    return dict.__repr__(self)


# make CommentedMap repr much more concise
CommentedMap.__repr__ = __as_dict_repr__  # type: ignore


def _mapCtor(self):
    if hasattr(self, "base_dir"):
        return make_map_with_base(self, self.base_dir, CommentedMap)
    return CommentedMap


CommentedMap.mapCtor = property(_mapCtor)  # type: ignore

# we can't subclass CommentedMap so we need to monkey patch
__base__deepcopy__ = CommentedMap.__deepcopy__


def __deepcopy__(self, memo):
    cp = __base__deepcopy__(self, memo)
    if hasattr(self, "base_dir"):
        setattr(cp, "base_dir", self.base_dir)
    return cp


if CommentedMap.__deepcopy__ is not __deepcopy__:
    CommentedMap.__deepcopy__ = __deepcopy__  # type: ignore


def _mapCtor2(self):
    if hasattr(self, "base_dir"):
        return make_map_with_base(self, self.base_dir, AnsibleMapping)
    return AnsibleMapping


AnsibleMapping.mapCtor = property(_mapCtor2)  # type: ignore


def copy(src):
    cls = getattr(src, "mapCtor", src.__class__)
    return cls(src)


def make_map_with_base(doc, baseDir, cls):
    loadTemplate = getattr(doc, "loadTemplate", None)
    _anchorCache = getattr(doc, "_anchorCache", None)
    lc = getattr(doc, "lc", None)

    def factory(*args, **kws):
        map = cls(*args, **kws)
        map.base_dir = baseDir
        if loadTemplate:
            map.loadTemplate = loadTemplate
        if _anchorCache is not None:
            map._anchorCache = _anchorCache
        if lc is not None and hasattr(map, "lc"):
            # map and doc might be different types
            map.lc.line = lc.line
            map.lc.col = lc.col
        return map

    return factory


# XXX?? because json keys are strings allow number keys to merge with lists
# other values besides delete not supported because current code can leave those keys in final result
mergeStrategyKey = "+%"  # supported values: "whiteout", "nullout", "merge", "error"

MappingMergeStrategy = Union[str, Callable[[str, Mapping, Mapping], Mapping]]

def merge_dicts(
    b: Mapping,
    a: Mapping,
    cls=None,
    replaceKeys=None,
    defaultStrategy: MappingMergeStrategy="merge",
    listStrategy="append_unique",
    replaceStrategy: MappingMergeStrategy="replace",
    mergeStrategyKey: str=mergeStrategyKey
) -> dict:
    """
    Returns a new dict (or cls) that recursively merges b into a.
    b is base, a overrides.

    Similar to https://yaml.org/type/merge.html but does a recursive merge.
    List values are appended if the value is not already present in the base list.
    If a list of replace keys is provided, those keys' values are replaced instead of merged.
    """
    if cls is not dict:
        cls = getattr(b, "mapCtor", cls or b.__class__)
    cp: dict = cls()
    skip = []
    for key, val in a.items():
        if key == mergeStrategyKey:
            continue
        if replaceKeys and key in replaceKeys:
            childStrategy = replaceStrategy
        else:
            childStrategy = "merge"
        # note: for merging treat None as an empty map
        if isinstance(val, Mapping) or val is None:
            strategy = val and val.get(mergeStrategyKey)
            if key in b:
                bval = b[key]
                if isinstance(bval, Mapping):
                    # merge strategy of a overrides b
                    if not strategy:
                        strategy = bval.get(mergeStrategyKey) or defaultStrategy
                    if strategy == "merge":
                        if not val:  # empty map, treat as missing key
                            continue
                        cp[key] = merge_dicts(
                            bval,
                            val,
                            cls,
                            defaultStrategy=childStrategy,
                            replaceKeys=replaceKeys,
                            replaceStrategy=replaceStrategy,
                            listStrategy=listStrategy,
                            mergeStrategyKey=mergeStrategyKey,
                        )
                        continue
                    if strategy == "error":
                        raise UnfurlError(
                            f'merging {key} is not allowed, "{mergeStrategyKey}: error" was set'
                        )
                    if callable(strategy):
                        if val is None:  # empty map, treat as missing key
                            continue
                        cp[key] = strategy(key, bval, val)
                        continue
                # otherwise we ignore bval because key is already in a
            if strategy == "whiteout":
                skip.append(key)
                continue
            if strategy == "nullout":
                val = None
        elif isinstance(val, MutableSequence) and key in b:
            bval = b[key]
            if isinstance(bval, MutableSequence):
                cp[key] = _merge_lists(
                    val,
                    bval,
                    cls,
                    childStrategy,
                    replaceKeys,
                    listStrategy,
                    mergeStrategyKey
                )
                continue
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


def _find_key_in_list(seq, key):
    for i, item in enumerate(seq):
        if isinstance(item, Mapping) and len(item) == 1 and key in item:
            return i
    return -1


def _merge_lists(
    val,
    bval,
    cls,
    childStrategy,
    replaceKeys,
    listStrategy,
    mergeStrategyKey
):
    if listStrategy == "append_unique":
        # XXX allow more strategies beyond append
        #     if appendlists == 'all' or key in appendlists:
        bval = bval[:]
        for i, item in enumerate(val):
            if isinstance(item, Mapping):
                if item.get(mergeStrategyKey) == "merge":
                    if i >= len(bval):
                        bval.append(item)
                    else:
                        bitem = bval[i]
                        if isinstance(bitem, Mapping):
                            bval[i] = merge_dicts(
                                bitem,
                                item,
                                cls,
                                defaultStrategy=childStrategy,
                                replaceKeys=replaceKeys,
                                listStrategy=listStrategy,
                                mergeStrategyKey=mergeStrategyKey
                            )
                        else:
                            bval[i] = item
                    continue
                elif len(item) == 1:
                    key = next(iter(item))
                    index = _find_key_in_list(bval, key)
                    if index > -1:
                        bval[index] = merge_dicts(
                            bval[index],
                            item,
                            cls,
                            defaultStrategy=childStrategy,
                            replaceKeys=replaceKeys,
                            listStrategy=listStrategy,
                            mergeStrategyKey=mergeStrategyKey
                        )
                        continue
            if item not in bval:
                bval.append(item)
    #   elif mergelists == 'all' or key in mergelists:
    #       newlist = []
    #       for ai, bi in zip(val, bval):
    #         if isinstance(ai, Mapping) and isinstance(bi, Mapping):
    #           newlist.append(mergeDicts(bi, ai, cls))
    #         elif a1 != deletemarker:
    #           newlist.append(a1)
    #       return newlist
    return bval


def _cache_anchors(_anchorCache, obj):
    if not hasattr(obj, "yaml_anchor"):
        return
    anchor = obj.yaml_anchor()
    if anchor and anchor.value:
        _anchorCache[anchor.value] = obj
        # By default, anchors are only emitted when it detects a reference
        # to an object previously seen, so force it to be emitted
        anchor.always_dump = True

    for value in obj.values() if isinstance(obj, dict) else obj:
        if isinstance(value, CommentedBase):
            _cache_anchors(_anchorCache, value)


def find_anchor(doc, anchorName):
    if not isinstance(doc, dict):
        return None

    _anchorCache = getattr(doc, "_anchorCache", None)
    if _anchorCache is None:
        _anchorCache = {}
        # recursively find anchors
        _cache_anchors(_anchorCache, doc)
        setattr(doc, "_anchorCache", _anchorCache)

    return _anchorCache.get(anchorName)


def _json_pointer_unescape(s: str) -> str:
    return s.replace("~1", "/").replace("~0", "~")


_RE_INVALID_JSONPOINTER_ESCAPE = re.compile("~[^01]|(~$)")


def _json_pointer_validate(pointer):
    invalid_escape = _RE_INVALID_JSONPOINTER_ESCAPE.search(pointer)
    if invalid_escape:
        raise UnfurlError(
            "Found invalid escape {} in JSON pointer {}".format(
                invalid_escape.group(), pointer
            )
        )
    return None


def get_template(
    doc: MutableMapping, key: "MergeKey", value, path, cls, includes=None, mergeStrategyKey=mergeStrategyKey
) -> Optional[Any]:
    template = doc
    templatePath = None
    if key.include:
        value, template, baseDir = doc.loadTemplate(  # type: ignore
            value, key.maybe, doc, path, key.include[len("include") :]
        )
        if template is None:  # include wasn't not found and key.maybe
            return None
        cls = make_map_with_base(doc, baseDir, cls)
        # fileKey = key._replace(include=None)
        # template = getTemplate(template, fileKey, "raw", (), cls)
    else:
        result = _find_template(doc, key, path, cls, not key.maybe)
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
                assert templatePath is not None
                prefix = list(
                    itertools.takewhile(lambda x: x[0] == x[1], zip(path, templatePath))
                )
                if len(prefix) == len(templatePath):
                    raise UnfurlError(
                        f'recursive include "{templatePath}" in "{path}" when including {key.key}'
                    )
            if includes is None:
                includes = CommentedMap()
            template = expand_dict(doc, path, includes, template, cls, mergeStrategyKey)
    finally:
        if key.include:
            # pop baseDir
            doc.loadTemplate(baseDir)  # type: ignore
    return template


def _find_template(
    doc: MutableMapping, key: "MergeKey", path, cls, fail: bool
) -> Optional[Tuple[Any, Optional[List[str]]]]:
    template: Optional[MutableMapping] = doc
    templatePath: Optional[list] = None
    if key.anchor:
        template = find_anchor(doc, key.anchor)
        if template is None:
            if not fail:
                return None
            else:
                raise UnfurlError(f"could not find anchor '{key.anchor}'")
        # XXX we don't know the path to the anchor so we can't support relative paths also
    elif key.relative:
        stop = None
        if key.relative - 1:
            stop = (key.relative - 1) * -1
        if templatePath is None:
            templatePath = list(path[:stop])
        else:
            templatePath = templatePath[:stop]
        template = lookup_path(doc, templatePath, cls)
        if template is None:
            if not fail:
                return None
            else:
                raise UnfurlError(
                    f"could not find relative path '{('.' * key.relative)}'"
                )
    for index, segment in enumerate(key.pointer):
        if isinstance(template, Sequence):
            # Array indexes should be turned into integers
            try:  # type: ignore # unreachable
                segment = int(segment)
            except ValueError:
                pass
        try:
            template = template[segment]  # type: ignore
        except (TypeError, LookupError):
            if not fail:
                return None
            raise UnfurlError(
                'can not find "%s" in document' % key.pointer[: index + 1].join("/")
            )

        if templatePath is not None:
            templatePath.append(segment)
    return template, templatePath


def has_template(doc: dict, key: "MergeKey", value, path, cls) -> bool:
    if key.include:
        if key.maybe:
            # treat missing includes as null template instead of an error
            return True
        loadTemplate = getattr(doc, "loadTemplate", None)
        if not loadTemplate:
            return False
        return doc.loadTemplate(  # type: ignore
            value, key.maybe, doc, True, key.include[len("include") :]
        )
    return _find_template(doc, key, path, cls, False) is not None


class _MissingInclude:
    def __init__(self, key: "MergeKey", value):
        self.key = key
        self.value = value

    # only include key so the doc will doc look like the original string (for round-trip parsing)
    def __repr__(self):
        return self.key.key


RE_FIRST = re.compile(r"([?]?)(include[\w-]*)?([*]\S+)?([.]+$)?")

MergeKey = namedtuple("MergeKey", "key, maybe, include, anchor, relative, pointer")


def parse_merge_key(key: str) -> Optional[MergeKey]:
    r"""
    +[maybe]?[include]?[anchor]?[relative]?[jsonpointer]?

    [maybe] = "?"
    [include] = "include"[\w-]*
    [anchor] = "*"[anchorname]
    relative = '.'+
    """
    original = key
    key = key[1:]
    _json_pointer_validate(key)
    parts = [_json_pointer_unescape(part) for part in key.split("/")]
    first = parts.pop(0)
    match = RE_FIRST.match(first)
    if not match:
        return None
    maybe, include, anchor, relative = match.groups()
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


def _copy_item(value):
    if isinstance(value, MutableMapping):
        return copy_dict(value, type(value))
    elif isinstance(value, (list, tuple)):
        return copy_list(value, type(value))
    else:
        return value


def copy_list(current: Sequence, cls=list) -> Sequence:
    return cls(_copy_item(i) for i in current)


def copy_dict(current: Mapping, cls=dict) -> MutableMapping:
    cp = cls()
    for key, value in current.items():
        cp[key] = _copy_item(value)
    return cp


def expand_dict(doc, path, includes, current, cls=dict, mergeStrategyKey=mergeStrategyKey) -> Any:
    """
    Return a copy of `doc` that expands include directives.
    Include directives look like "+path/to/value"
    When appearing as a key in a map it will merge the result with the current dictionary.
    If the result isn't a map, that value will be returned instead.
    When appearing in a list it will insert the result in the list;
    if result is also a list, each item will be inserted separately.
    (If you don't want that behavior just wrap include in another list, e.g "[+list1]")
    """
    cp = cls()
    # first merge any includes includes into cp
    templates: List[MutableMapping] = []
    assert isinstance(current, Mapping), current
    for key, value in current.items():
        if not isinstance(key, str):
            cp[key] = value
            continue
        if key.startswith(mergeStrategyKey[0]):
            if key == mergeStrategyKey:
                cp[key] = value
                continue
            if len(key) == 2 and key[1] == "&":
                if hasattr(doc, "_anchorCache"):
                    doc._anchorCache[value] = cp
                continue
            mergeKey = parse_merge_key(key)
            if not mergeKey:
                cp[key] = value
                continue
            foundTemplate = has_template(doc, mergeKey, value, path, cls)
            if not foundTemplate:
                includes.setdefault(path, []).append(_MissingInclude(mergeKey, value))
                cp[key] = value
                continue
            includes.setdefault(path, []).append((mergeKey, value))
            template = get_template(doc, mergeKey, value, path, cls, includes, mergeStrategyKey)
            if isinstance(template, MutableMapping):
                templates.append(template)
            elif mergeKey.include and template is None:
                continue  # include path not found
            else:
                if len(current) > 1:  # XXX include merge directive keys in count
                    raise UnfurlError(
                        f"can not merge {mergeKey} with non-map value of type {type(template)}: {template}"
                    )
                else:
                    return template  # current dict is replaced with a value
        # elif key.startswith("q+"):
        #    cp[key[2:]] = value
        elif isinstance(value, Mapping):
            cp[key] = expand_dict(doc, path + (key,), includes, value, cls, mergeStrategyKey)
        elif isinstance(value, list):
            cp[key] = list(expand_list(doc, path + (key,), includes, value, cls, mergeStrategyKey))
        else:
            cp[key] = value

    if templates:
        accum = templates.pop(0)
        templates.append(cp)
        while templates:
            cls = getattr(templates[0], "mapCtor", cls)
            accum = merge_dicts(accum, templates.pop(0), cls, mergeStrategyKey)
        return accum
    else:
        return cp
    # e,g, merge_dicts(merge_dicts(a, b), cp)
    # return includes, reduce(lambda accum, next: merge_dicts(accum, next, cls), templates, {}), cp


def _find_missing_includes(includes):
    for x in includes.values():
        for i in x:
            if isinstance(i, _MissingInclude):
                yield i


def _delete_deleted_keys(expanded, mergeStrategyKey):
    for key in list(expanded):
        value = expanded[key]
        if isinstance(value, Mapping):
            if value.get(mergeStrategyKey) in ["whiteout", "nullout"]:
                del expanded[key]
            else:
                _delete_deleted_keys(value, mergeStrategyKey)


def expand_doc(doc, current=None, cls=dict, mergeStrategyKey=mergeStrategyKey) -> Tuple[MutableMapping, Any]:
    includes = cls()
    if current is None:
        current = doc
    if not isinstance(doc, Mapping) or not isinstance(current, Mapping):
        raise UnfurlError(f"top level element {doc} is not a dict")
    expanded = expand_dict(doc, (), includes, current, cls, mergeStrategyKey)
    assert isinstance(expanded, Mapping), expanded
    if hasattr(doc, "_anchorCache"):
        setattr(expanded, "_anchorCache", doc._anchorCache)
    last = 0
    while True:
        missing = list(_find_missing_includes(includes))
        if len(missing) == 0:
            # remove any stray merge directive that weren't applied
            _delete_deleted_keys(expanded, mergeStrategyKey)
            return includes, expanded
        if len(missing) == last:  # no progress
            required_missing = [
                f"{i.key.key}:{i.value or ''}" for i in missing if not i.key.maybe
            ]
            if required_missing:
                raise UnfurlError(f"missing includes: {required_missing}")
            # delete missing includes directives
            for path, _includes in includes.items():
                for i in _includes:
                    if i in missing:
                        if path:
                            delete_path(expanded, path)
                        else:
                            del expanded[str(i)]  # type: ignore
            return includes, expanded
        last = len(missing)
        includes = CommentedMap()
        expanded = expand_dict(expanded, (), includes, current, cls, mergeStrategyKey)
        assert isinstance(expanded, Mapping), expanded
        if hasattr(doc, "_anchorCache"):
            setattr(expanded, "_anchorCache", doc._anchorCache)


def expand_list(doc, path, includes, value, cls=dict, mergeStrategyKey=mergeStrategyKey):
    for i, item in enumerate(value):
        if isinstance(item, Mapping):
            if item.get(mergeStrategyKey) == "whiteout":
                continue
            newitem = expand_dict(doc, path + (i,), includes, item, cls, mergeStrategyKey)
            if isinstance(newitem, MutableSequence):
                for i in newitem:
                    yield i
            else:
                yield newitem
        else:
            yield item


def diff_dicts(old, new, cls=dict, skipkeys=()):
    """
    given old, new return diff where merge_dicts(old, diff) == new
    """
    diff = cls()
    # start with old to preserve original order
    for key, oldval in old.items():
        if key in skipkeys:
            continue
        if key in new:
            newval = new[key]
            if oldval != newval:
                if isinstance(oldval, Mapping):
                    if isinstance(newval, Mapping):
                        _diff = diff_dicts(oldval, newval, cls, skipkeys)
                        if not skipkeys or _diff:
                            diff[key] = _diff
                    elif newval is None:
                        # dicts merge with None so add a nullout directive to preserve the None
                        diff[key] = cls((("+%", "nullout"),))
                    else:  # new non-dict val replaces old dict
                        diff[key] = newval
                else:
                    diff[key] = newval
        else:  # not in new, so add a whiteout directive to delete this key
            diff[key] = cls((("+%", "whiteout"),))

    for key in new:
        if key in skipkeys:
            continue
        if key not in old:
            diff[key] = new[key]
    return diff


# XXX rename function, confusing name
def patch_dict(old: MutableMapping, new: Mapping, preserve=False) -> MutableMapping:
    """
    Transform old into new based on object equality while preserving as much of old object as possible.

    If ``preserve`` is True ``new`` will be merged in without removing ``old`` items.
    """
    # start with old to preserve original order
    for key, val in list(old.items()):
        if key in new:
            newval = new[key]
            if val != newval:
                if isinstance(val, MutableMapping) and isinstance(newval, Mapping):
                    old[key] = patch_dict(val, newval, preserve)
                elif isinstance(val, MutableSequence) and isinstance(
                    newval, MutableSequence
                ):
                    if preserve:
                        # add new items that don't appear in old list
                        old[key] = val + [item for item in newval if item not in val]  # type: ignore
                    else:
                        # preserve old item in list if they are equal to the new item
                        old[key] = [
                            (val[val.index(item)] if item in val else item)
                            for item in newval
                        ]
                else:
                    old[key] = newval
        elif not preserve:
            del old[key]

    for key in new:
        if key not in old:
            old[key] = new[key]

    return old


def intersect_dict(old, new, cls=dict):
    """
    remove keys from old that don't match new
    """
    # start with old to preserve original order
    for key, val in list(old.items()):
        if key in new:
            newval = new[key]
            if val != newval:
                if isinstance(val, Mapping) and isinstance(newval, Mapping):
                    old[key] = intersect_dict(val, newval, cls)
                else:
                    del old[key]
        else:
            del old[key]

    return old


def lookup_path(doc, path, cls=dict):
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


def replace_path(doc, key, value, cls=dict):
    path = key[:-1]
    last = key[-1]
    ref = lookup_path(doc, path, cls)
    ref[last] = value


def delete_path(doc, key):
    if key:
        path = key[:-1]
        last = key[-1]
        ref = lookup_path(doc, path)
        if ref:
            del ref[last]


def add_template(changedDoc, path, mergeKey, template, cls):
    # if includeKey.anchor: #???
    if mergeKey.relative:
        if mergeKey.relative > 1:
            path = path[: (mergeKey.relative - 1) * -1]
        current = lookup_path(changedDoc, path, cls)
    else:
        current = changedDoc

    assert mergeKey.pointer
    path = mergeKey.pointer[:-1]
    last = mergeKey.pointer[-1]
    for segment in path:
        current = current.setdefault(segment, {})
    current[last] = template


def restore_includes(includes, originalDoc, changedDoc, cls=dict):
    """
    Modifies changedDoc with to use the includes found in originalDoc
    """
    # if the path to the include still exists
    # resolve the include
    # if the include doesn't exist in the current doc, re-add it
    # create a diff between the current object and the merged includes
    expandedOriginalIncludes, expandedOriginalDoc = expand_doc(originalDoc, cls=cls)
    for key, value in includes.items():
        ref = lookup_path(changedDoc, key, cls)
        if ref is None:
            # inclusion point no longer exists
            continue

        mergedIncludes: Mapping = {}
        for includeKey, includeValue in value:
            if includeKey.include:
                ref = None
                continue
            stillHasTemplate = has_template(
                changedDoc, includeKey, includeValue, key, cls
            )
            if stillHasTemplate:
                template = get_template(changedDoc, includeKey, includeValue, key, cls)
            else:
                if has_template(originalDoc, includeKey, includeValue, key, cls):
                    template = get_template(
                        originalDoc, includeKey, includeValue, key, cls
                    )
                else:
                    template = get_template(
                        expandedOriginalDoc, includeKey, includeValue, key, cls
                    )

            if not isinstance(ref, MutableMapping):
                # XXX3 if isinstance(ref, list) lists not yet implemented
                if ref == template:
                    # ref still resolves to the template's value so replace it with the include
                    replace_path(changedDoc, key, {includeKey.key: includeValue}, cls)
                # ref isn't a map anymore so can't include a template
                break

            if not isinstance(template, Mapping):
                # ref no longer includes that template or we don't want to save it
                continue
            else:
                mergedIncludes = merge_dicts(mergedIncludes, template, cls)
                ref[includeKey.key] = includeValue

            if not stillHasTemplate:
                if includeValue != "raw":
                    if has_template(originalDoc, includeKey, includeValue, key, cls):
                        template = get_template(
                            originalDoc, includeKey, "raw", key, cls
                        )
                    else:
                        template = get_template(
                            expandedOriginalDoc, includeKey, "raw", key, cls
                        )
                add_template(changedDoc, key, includeKey, template, cls)

        if isinstance(ref, Mapping):
            diff = diff_dicts(mergedIncludes, ref, cls)
            replace_path(changedDoc, key, diff, cls)
