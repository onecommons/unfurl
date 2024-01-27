"""
This module contains utility functions that can be executed in "spec" mode (e.g. as part of a class definition or in ``_class_init_``)
and in the safe mode Python sandbox.
Each of these are also available as Eval `Expression Functions`.
"""
import base64
import hashlib
import math
import random
import string
import re
from typing import (
    TYPE_CHECKING,
    Iterator,
    List,
    Mapping,
    MutableMapping,
    Tuple,
    Union,
    cast,
    Dict,
    Optional,
    Any,
    NewType,
    overload,
)
from typing_extensions import TypedDict, Unpack
from urllib.parse import quote, quote_plus


def _digest(arg: str, case: str, digest: Optional[str] = None) -> str:
    m = hashlib.sha1()  # use same digest function as git
    m.update(arg.encode("utf-8"))
    if digest:
        m.update(digest.encode("utf-8"))
    if case == "any":
        digest = (
            base64.urlsafe_b64encode(m.digest())
            .decode()
            .strip("=")
            .replace("_", "")
            .replace("-", "")
        )
    else:
        digest = base64.b32encode(m.digest()).decode().strip("=")
        if case == "lower":
            return digest.lower()
        elif case == "upper":
            return digest.upper()
    return digest


def _mid_truncate(label: str, replace: str, trunc: int) -> str:
    if len(label) > trunc:
        replace_len = len(replace)
        mid = (trunc - replace_len) / 2
        if mid <= 4:
            return label[:trunc]
        # trunc is odd, take one more from the beginning
        return label[: math.ceil(mid)] + replace + label[-math.floor(mid) :]
    return label


LabelArg = Union[str, Mapping, list]


class DNSLabelKwArgs(TypedDict, total=False):
    start_prepend: str
    sep: str
    digest: Optional[str]
    digestlen: int


class LabelKwArgs(DNSLabelKwArgs, total=False):
    start: str
    allowed: str
    replace: str
    end: Optional[str]
    max: int
    case: str


_label_defaults = LabelKwArgs(
    allowed=r"\w",
    max=63,
    case="any",
    replace="",
    start="a-zA-Z",
    start_prepend="x",
    sep="",
    digest=None,
    digestlen=-1,
)


def _validate_allowed(chars):
    # avoid common footgun
    if chars and chars[0] == "[" and chars[-1] == "]":
        return chars[1:-1]
    else:
        return chars


# mypy: enable-incomplete-feature=Unpack


@overload
def to_label(arg: Union[str, list], **kw: Unpack[LabelKwArgs]) -> str:
    ...


@overload
def to_label(arg: Mapping, **kw: Unpack[LabelKwArgs]) -> dict:
    ...


def to_label(arg: LabelArg, **kw: Unpack[LabelKwArgs]):
    r"""Convert a string to a label with the given constraints.
        If a dictionary, all keys and string values are converted.
        If list, to_label is applied to each item and concatenated using ``sep``

    Args:
        arg (str or dict or list): Convert to label
        allowed (str, optional): Allowed characters. Regex character ranges and character classes.
                               Defaults to "\w"  (equivalent to ``[a-zA-Z0-9_]``)
        replace (str, optional): String Invalidate. Defaults to "" (remove the characters).
        start (str, optional): Allowed characters for the first character. Regex character ranges and character classes.
                               Defaults to "a-zA-Z"
        start_prepend (str, optional): If the start character is invalid, prepend with this string (Default: "x")
        end (str, optional): Allowed trailing characters. Regex character ranges and character classes.
                            If set, invalid characters are stripped.
        max (int, optional): max length of label. Defaults to 63 (the maximum for a DNS name).
        case (str, optional): "upper", "lower" or "any" (no conversion). Defaults to "any".
        sep (str, optional): Separator to use when concatenating a list. Defaults to ""
        digestlen (int, optional): If a label is truncated, the length of the digest to include in the label. 0 to disable.
                                Default: 3 or 2 if max < 32
    """
    case: str = kw.get("case", _label_defaults["case"])
    sep: str = kw.get("sep", _label_defaults["sep"])
    digest: Optional[str] = kw.pop("digest", _label_defaults["digest"])
    replace: str = kw.get("replace", _label_defaults["replace"])
    elide_chars = replace

    if isinstance(arg, Mapping):
        # convert keys and string values of the mapping, filtering out nulls
        # only apply digest to values
        return {
            to_label(n, **kw): to_label(v, digest=digest, **kw)  # type: ignore
            for n, v in arg.items()
            if v is not None
        }

    trunc = int(kw.pop("max", _label_defaults["max"]))
    assert trunc >= 1, trunc
    checksum: int = kw.pop("digestlen", _label_defaults["digestlen"])

    if checksum == -1:
        checksum = 2 if trunc < 32 else 3
    if checksum:
        maxchecksum = min(trunc, checksum)
    else:
        maxchecksum = 0
    if isinstance(arg, list):
        if not arg:
            return ""
        # concatentate list
        # adjust max for length of separators
        trunc_chars = trunc - min(len(sep) * (len(arg) - 1), trunc - 1)
        seg_max = max(trunc_chars // len(arg), 1)
        segments = [str(n) for n in arg]
        labels = [to_label(n, digestlen=0, max=9999, **kw) for n in segments]  # type: ignore
        length = sum(map(len, labels))
        if length > trunc_chars or digest is not None:
            # needs truncation and/or digest
            # redistribute space from short segments
            leftover = sum(map(lambda n: max(seg_max - len(n), 0), labels))
            seg_max += leftover // len(labels)
            labels = [_mid_truncate(seg, elide_chars, seg_max) for seg in labels]
            if checksum:
                # one of the labels was truncated, add a digest
                trunc -= len(sep) + maxchecksum
                hash = _digest("".join(segments), case, digest)[:maxchecksum]
                if trunc <= 0:
                    return hash
                return sep.join(labels)[:trunc] + sep + hash
        return sep.join(labels)[:trunc]
    elif isinstance(arg, str):
        start: str = _validate_allowed(kw.get("start", _label_defaults["start"]))
        start_prepend: str = kw.get("start_prepend", _label_defaults["start_prepend"])
        allowed: str = _validate_allowed(kw.get("allowed", _label_defaults["allowed"]))
        end: Optional[str] = kw.get("end")

        if arg and re.match(rf"[^{start}]", arg[0]):
            val = start_prepend + arg
        else:
            val = arg
        if len(val) > trunc:
            # heuristic: if we need to truncate, just remove the invalid characters
            replace = ""
        val = re.sub(rf"[^{allowed}]", replace, val)
        if end is not None:
            while val and re.match(rf"[^{_validate_allowed(end)}]", val[-1]):
                val = val[:-1]
        if case == "lower":
            val = val.lower()
        elif case == "upper":
            val = val.upper()

        if maxchecksum and (digest is not None or len(val) > trunc):
            trunc -= min(trunc, maxchecksum)
            return (
                _mid_truncate(val, elide_chars, trunc)
                + _digest(arg, case, digest)[:maxchecksum]
            )
        else:
            return _mid_truncate(val, elide_chars, trunc)
    else:
        return arg


@overload
def to_dns_label(
    arg: Union[str, list],
    *,
    allowed=r"[a-zA-Z0-9-]",
    start="[a-zA-Z]",
    replace="--",
    case="lower",
    end="[a-zA-Z0-9]",
    max=63,
    **kw: Unpack[DNSLabelKwArgs],
) -> str:
    ...


@overload
def to_dns_label(
    arg: Mapping,
    *,
    allowed=r"[a-zA-Z0-9-]",
    start="[a-zA-Z]",
    replace="--",
    case="lower",
    end="[a-zA-Z0-9]",
    max=63,
    **kw: Unpack[DNSLabelKwArgs],
) -> dict:
    ...


def to_dns_label(
    arg: LabelArg,
    *,
    allowed=r"[a-zA-Z0-9-]",
    start="[a-zA-Z]",
    replace="--",
    case="lower",
    end="[a-zA-Z0-9]",
    max=63,
    **kw: Unpack[DNSLabelKwArgs],
):
    """
    Convert the given argument (see :py:func:`unfurl.tosca_plugins.functions.to_label` for full description) to a DNS label (a label is the name separated by "." in a domain name).
    The maximum length of each label is 63 characters and can include
    alphanumeric characters and hyphens but a domain name must not commence or end with a hyphen.

    Invalid characters are replaced with "--".
    """
    return to_label(
        arg,
        allowed=allowed,
        start=start,
        replace=replace,
        case=case,
        end=end,
        max=max,
        **kw,
    )


@overload
def to_kubernetes_label(
    arg: Union[str, list],
    *,
    allowed=r"\w.-",
    case="any",
    replace="__",
    start="[a-zA-Z0-9]",
    end="[a-zA-Z0-9]",
    max=63,
    **kw: Unpack[DNSLabelKwArgs],
) -> str:
    ...


@overload
def to_kubernetes_label(
    arg: Mapping,
    *,
    allowed=r"\w.-",
    case="any",
    replace="__",
    start="[a-zA-Z0-9]",
    end="[a-zA-Z0-9]",
    max=63,
    **kw: Unpack[DNSLabelKwArgs],
) -> dict:
    ...


def to_kubernetes_label(
    arg: LabelArg,
    *,
    allowed=r"\w.-",
    case="any",
    replace="__",
    start="[a-zA-Z0-9]",
    end="[a-zA-Z0-9]",
    max=63,
    **kw: Unpack[DNSLabelKwArgs],
):
    """
    See https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/#syntax-and-character-set

    Invalid characters are replaced with "__".
    """
    return to_label(
        arg,
        allowed=allowed,
        replace=replace,
        end=end,
        max=max,
        case=case,
        start=start,
        **kw,
    )


@overload
def to_googlecloud_label(
    arg: Union[str, list],
    *,
    allowed=r"\w-",
    case="lower",
    replace="__",
    start="[a-zA-Z]",
    max=63,
    **kw: Unpack[DNSLabelKwArgs],
) -> str:
    ...


@overload
def to_googlecloud_label(
    arg: Mapping,
    *,
    allowed=r"\w-",
    case="lower",
    replace="__",
    start="[a-zA-Z]",
    max=63,
    **kw: Unpack[DNSLabelKwArgs],
) -> dict:
    ...


def to_googlecloud_label(
    arg: LabelArg,
    *,
    allowed=r"\w-",
    case="lower",
    replace="__",
    start="[a-zA-Z]",
    max=63,
    **kw: Unpack[DNSLabelKwArgs],
):
    """
    See https://cloud.google.com/resource-manager/docs/labels-overview

    Invalid characters are replaced with "__".
    """
    return to_label(
        arg, allowed=allowed, case=case, replace=replace, max=max, start=start, **kw
    )


def get_random_password(
    count=12,
    prefix="uv",
    extra=None,
    valid_chars="",
    start=string.ascii_letters,
):
    srandom = random.SystemRandom()
    if not valid_chars:
        valid_chars = string.ascii_letters + string.digits
    if extra is None:
        extra = "%()*+,-./<>?=^_~"
    if not start:
        start = valid_chars
    source = valid_chars + extra
    return prefix + "".join(
        srandom.choice(source if i else start) for i in range(count)
    )


def generate_string(preset="", len=0, ranges=(), **kw) -> str:
    # must match https://github.com/onecommons/unfurl-gui/blob/main/packages/oc-pages/vue_shared/lib/directives/generate.js
    if preset == "number":
        return get_random_password(len or 1, valid_chars=string.digits, start="")
    elif preset == "password":
        pw = ""
        while not re.search(r"\d", pw):
            pw = get_random_password(len or 15, "", "", start="")
        return pw
    else:
        extra = None
        valid_chars = ""
        if ranges:
            for interval in ranges:
                if isinstance(interval[0], str):
                    interval = [ord(interval[0]), ord(interval[1])]
                valid_chars += "".join([chr(i) for i in range(*interval)])
            extra = ""
        # default includes some punctuation (the extra param)
        return get_random_password(len or 10, "", extra, valid_chars, start="")


def urljoin(
    scheme: str,
    host: str,
    port: Union[str, int, None] = None,
    path: Optional[str] = None,
    query: Optional[str] = None,
    frag: Optional[str] = None,
) -> Optional[str]:
    """
    Evaluate a list of url components to a relative or absolute URL,
    where the list is ``[scheme, host, port, path, query, fragment]``.

    The list must have at least two items (``scheme`` and ``host``) present
    but if either or both are empty a relative or scheme-relative URL is generated.
    If all items are empty, ``null`` is returned.
    The ``path``, ``query``, and ``fragment`` items are url-escaped if present.
    Default ports (80 and 443 for ``http`` and ``https`` URLs respectively) are omitted even if specified.
    """
    if not scheme and not host and not path and not query and not frag:
        return None

    if port and (
        not (scheme == "https" and int(port) == 443)
        and not (scheme == "http" and int(port) == 80)
    ):
        netloc = f"{host}:{port}"
    else:
        # omit default ports
        netloc = host or ""
    if path:
        path = quote(path)
    if netloc and path and path[0] != "/":
        path = "/" + path
    if query:
        query = "?" + quote_plus(query)
    else:
        query = ""
    if frag:
        frag = "#" + quote(frag)
    else:
        frag = ""

    prefix = ""  # relative url
    if scheme:
        # absolute url or relative url with scheme
        prefix = scheme + ":"
    if netloc:
        # its an absolute url or scheme-relative url if scheme is missing
        prefix += "//"

    return prefix + netloc + (path or "") + query + frag


__all__ = [
    "urljoin",
    "to_label",
    "to_dns_label",
    "to_kubernetes_label",
    "to_googlecloud_label",
    "generate_string",
]
