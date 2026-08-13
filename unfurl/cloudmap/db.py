# Copyright (c) 2026 Adam Souzis
# SPDX-License-Identifier: MIT
"""The cloud map document itself: reading, querying and writing ``cloudmap.yaml``.

:py:class:`CloudMapDB` is a standalone view over one document. It knows nothing
about repository hosts or the git repository the document lives in — see
:py:mod:`unfurl.cloudmap` for those.

:py:class:`CloudMapStore` is the interface it shares with
:py:class:`~unfurl.cloudmap.proxy.CloudMapProxy` — what the cloud map's *owner*
uses. Deliberately not in ``cloudmap_defs``, which custom analyzers can import:
this is the interface they must not reach. What they get is a
:py:class:`~unfurl.cloudmap.provenance.ProvenanceTrackingContext` wrapping a
store, implementing
:py:class:`~unfurl.tosca_plugins.cloudmap_defs.AnalyzerContext` and nothing more.
"""

import os
import os.path
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
    Union,
    cast,
)
from abc import abstractmethod
from typing_extensions import Literal
from urllib.parse import urljoin, urlparse, unquote

from ..logs import getLogger
from ..merge import _json_pointer_unescape
from ..support import ContainerImage
from ..tosca_plugins.cloudmap_defs import (
    CloudMapWriter,
    Artifact,
    ArtifactDict,
    CloudMapRecord,
    CloudMapView,
    CloudType,
    CloudTypeDict,
    Component,
    ComponentDict,
    Instantiation,
    Repository,
    RepositoryDict,
    Service,
    ServiceDict,
    get_repository_url,
)
from ..util import API_VERSION, UnfurlError
from ..yamlloader import YamlConfig
from .oci import create_oci_artifact

if TYPE_CHECKING:
    from . import CloudMap
    from ..localenv import LocalEnv

logger = getLogger("unfurl")


_basepath = os.path.abspath(os.path.dirname(__file__))

# maps the "record-type" of a CloudMap pseudo-URL (e.g. "service:https://example.com/")
# to the top-level section of the cloudmap document that the record lives in.
CLOUDMAP_RECORD_TYPES: Dict[str, str] = {
    "repository": "repositories",
    "artifact": "artifacts",
    "component": "components",
    "service": "services",
    "instantiation": "instantiations",
    "type": "types",
}

CLOUDMAP_REF_PREFIX = "cloudmap:"


def _find_matching_bracket(s: str) -> int:
    """Return the index of the "]" matching the "[" that starts ``s``, or -1.

    Brackets can be nested as long as they are balanced -- the closing "]" is
    the one that matches the opening "[".
    """
    depth = 0
    for i, c in enumerate(s):
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if not depth:
                return i
    return -1


def _split_cloudmap_keys(path: str) -> Optional[List[str]]:
    """Split the ``path`` of a CloudMap pseudo-URL into its keys.

    Keys are separated by "/" but a key delimited by "[" and "]" (an
    ``embedded-ref``) is taken verbatim, so that the "/" and "#" characters of
    a URL or a nested pseudo-URL aren't misread. Undelimited keys are
    percent-decoded.

    Returns None if ``path`` is malformed (unbalanced or misplaced brackets,
    or an empty key).
    """
    keys: List[str] = []
    while path:
        if path.startswith("["):
            end = _find_matching_bracket(path)
            if end < 0:
                return None
            key = path[1:end]
            if not key:
                return None  # an empty key
            rest = path[end + 1 :]
            if rest and not rest.startswith("/"):
                return None  # trailing characters after a delimited key
        else:
            key, sep, remainder = path.partition("/")
            if not key or "[" in key or "]" in key:
                return None  # an empty key or an undelimited bracket
            key = unquote(key)
            rest = sep + remainder
        keys.append(key)
        if not rest:
            return keys
        path = rest[1:]  # skip the "/"
    return None  # empty or trailing "/"


def extends_children(
    types: Mapping[str, Union[CloudType, Mapping[str, object]]],
) -> Dict[str, List[str]]:
    """Reverse the ``extends`` edges of a ``types`` section: parent -> subtypes.

    Accepts either the :class:`CloudType` instances a :class:`CloudMapDB`
    holds or the raw dicts of a parsed cloudmap document, so one adjacency
    serves the local database, the server's in-memory document and the
    proxy's cache alike.

    ``extends`` lists are often pre-flattened to the full ancestor closure
    and name the type itself; that self-edge is dropped so the walk
    terminates.
    """
    children: Dict[str, List[str]] = {}
    for name, record in types.items():
        if isinstance(record, CloudType):
            parents: Sequence[str] = record.extends
        else:
            raw = record.get("extends")
            if not isinstance(raw, list):
                continue
            parents = [p for p in raw if isinstance(p, str)]
        for parent in parents:
            if parent != name:
                children.setdefault(parent, []).append(name)
    return children


def subtype_closure(children: Mapping[str, Sequence[str]], type_name: str) -> Set[str]:
    """``type_name`` plus every type that (transitively) extends it.

    ``children`` is the adjacency from :func:`extends_children`.
    ``type_name`` need not have a type record of its own: an unknown name
    matches only itself, which is what keeps a type filter working against
    a document with no ``types`` section.
    """
    names = {type_name}
    queue = [type_name]
    while queue:
        for kid in children.get(queue.pop(), ()):
            if kid not in names:
                names.add(kid)
                queue.append(kid)
    return names


class CloudMapStore(CloudMapWriter):
    """A store of cloud map records, belonging to a :py:class:`CloudMap`."""

    __cloudmap: Optional["CloudMap"] = None

    def set_cloudmap(self, cloudmap: "CloudMap") -> None:
        """Attach this store to the cloud map that owns it.

        Assigned here rather than by the caller because ``__cloudmap`` is
        name-mangled to this class, so nothing outside it can reach the
        cloud map through a store.
        """
        self.__cloudmap = cloudmap

    @property
    def _local__env(self) -> Optional["LocalEnv"]:
        return self.__cloudmap.local_env if self.__cloudmap else None

    def analyze_url(
        self,
        url: str,
        analyze: Literal["yes", "no", "save-only", "default"] = "default",
        replace: bool = False,
    ) -> Optional[CloudMapRecord]:
        """Analysis needs to clone and inspect repositories, so it is the
        owning cloud map's job; a store that isn't attached to one can only
        keep records."""
        if self.__cloudmap is None:
            raise UnfurlError(
                f"{type(self).__name__} is not attached to a cloudmap, so it "
                "cannot analyze urls"
            )
        return self.__cloudmap.analyze_url(url, analyze, replace)

    @abstractmethod
    def save(self, msg: str = "", commit: bool = True) -> Any:
        """Persist the records added so far, describing them with ``msg``.

        ``commit`` asks a store whose repository is owned elsewhere -- a
        server-backed one -- to commit them rather than leave them staged. A
        local document is committed by its owner (see
        :py:meth:`unfurl.cloudmap.CloudMap.save`), so it ignores this.
        """


class CloudMapDB(CloudMapStore):
    """
    Loads the cloudmap yaml file
    """

    DEFAULT_NAME = "cloudmap.yml"

    def __init__(self, path=".", contents=None, validate: bool = True) -> None:
        self._load(path, contents, validate)

    @staticmethod
    def _normalize_url(url: str, section: str) -> Tuple[str, str, List[str]]:
        """Normalize a reference to a record to a key in the cloudmap database.

        Records can be referenced by a JSON-pointer-style url fragment
        (e.g. ``#/artifacts/<key>``) or by a CloudMap pseudo-URL that names the
        record's type (e.g. ``service:https://example.com/``). Both forms can be
        followed by a path of keys into the record and a pseudo-URL can name the
        cloudmap document that contains the record:

        .. code-block::

          #/instantiations/https:~1~1pipeline.com~1myrepo~134/instantiated
          instantiation:[https://pipeline.com/myrepo/34]/instantiated
          cloudmap:[git://github.com/cloudmap.git#:cloudmap.yaml]:service:[https://unfurl.cloud#1.2]

        Keys delimited with "[" and "]" are used verbatim; undelimited keys are
        percent-decoded (they have to escape "/" and "#").

        As a shorthand, psuedo-urls with only the initial record key do not need to be "[]" delimited:
        if the key is undelimited and contains a ":" or an "@", the
        entire remainder is returned as the key verbatim.

        Args:
            url: The reference to normalize. A reference that is neither form
                above is a key already, as is a malformed pseudo-URL (one with
                unbalanced brackets or empty keys); it is returned unchanged.
            section: The top-level section of the cloudmap document that the
                record lives in (e.g. "services").

        Returns:
            A ``(cloudmap_url, key, path)`` tuple, where ``cloudmap_url`` is the
            url of the cloudmap document given by the ``cloudmap:`` prefix (as in
            ``cloudmap:[<cloudmap_url>]:service:<key>``) or "" when the reference
            doesn't name one and so refers to this cloudmap; ``key`` is the
            record's key within ``section``; and ``path`` are the keys of the
            element referenced inside the record, empty when the reference names
            the whole record.
        """
        prefix = f"#/{section}/"
        if url.startswith(prefix):
            keys = [_json_pointer_unescape(k) for k in url[len(prefix) :].split("/")]
            return "", keys[0], keys[1:]
        cloudmap_url = ""
        ref = url
        if ref.startswith(CLOUDMAP_REF_PREFIX):
            ref = ref[len(CLOUDMAP_REF_PREFIX) :]
            if ref.startswith("["):
                # the cloudmap document containing the record is named explicitly
                end = _find_matching_bracket(ref)
                # end < 2 is a missing "]" or an empty url
                if end < 2 or not ref[end + 1 :].startswith(":"):
                    return "", url, []
                cloudmap_url = ref[1:end]
                ref = ref[end + 2 :]
        record_type, sep, rest = ref.partition(":")
        if not sep or not rest or CLOUDMAP_RECORD_TYPES.get(record_type) != section:
            return "", url, []
        if not rest.startswith("["):
            first = rest.partition("/")[0]
            if ":" in first or "@" in first:
                # opaque-key shorthand: the rest of the reference is the key
                return cloudmap_url, rest, []
        keys = _split_cloudmap_keys(rest) or []
        if not keys:
            return "", url, []
        return cloudmap_url, keys[0], keys[1:]

    def _matches_cloudmap_url(self, cloudmap_url: str) -> bool:
        """Return True if ``cloudmap_url`` refers to this cloudmap document.

        An empty url means the reference didn't name a document, so it refers
        to this one. Otherwise the url has to resolve to where this cloudmap
        came from (``self.path``):

        * a cloudmap loaded from a file matches a "file:" url or a plain path
          -- relative ones resolve against the cloudmap's directory -- naming
          that file or the directory containing it. Other schemes (e.g. a
          "git:" url) can't be matched against a local path, so they are
          treated as another cloudmap.
        * a cloudmap identified by a url (e.g. one a :class:`CloudMapProxy`
          mirrors) matches that url, or a relative url resolving to it.
        """
        if not cloudmap_url:
            return True
        if not self.path:
            return False  # this cloudmap isn't identified by a path or a url
        if cloudmap_url == self.path:
            return True
        if urlparse(self.path).scheme not in ("", "file"):
            # this cloudmap is identified by a url, not a local file
            return urljoin(self.path, cloudmap_url) == self.path
        parsed = urlparse(cloudmap_url)
        if parsed.scheme not in ("", "file") or parsed.netloc not in ("", "localhost"):
            return False
        if not parsed.path:
            return False  # a bare fragment or query isn't a document url
        # a "file:" url percent-encodes characters a bare path spells out
        path = unquote(parsed.path) if parsed.scheme == "file" else parsed.path
        referenced = os.path.join(os.path.dirname(self.path), path)
        if os.path.isdir(referenced):
            referenced = os.path.join(referenced, os.path.basename(self.path))
        # this cloudmap's path can be relative too (e.g. a path in a repository)
        return os.path.abspath(referenced) == os.path.abspath(self.path)

    def _get_key(self, url: str, section: str) -> Optional[str]:
        """Return the key of the record in ``section`` that ``url`` refers to.

        Returns None if the reference is to a record in another cloudmap
        document. A path of keys into the record is discarded -- the getters
        return whole records.
        """
        cloudmap_url, key, _path = self._normalize_url(url, section)
        if not self._matches_cloudmap_url(cloudmap_url):
            return None
        return key

    def get_repository(self, r: Union[str, Repository]) -> Optional[Repository]:
        """Get a repository by its key (URL)."""
        if isinstance(r, Repository):
            url = r.url
        else:
            key = self._get_key(r, "repositories")
            if key is None:
                return None
            url = get_repository_url(key)
        found = self.repositories.get(url)
        if not found and not url.endswith(".git"):
            # package ids don't have .git suffix, try adding it
            return self.repositories.get(url + ".git")
        return found

    def get_artifact(self, url: str) -> Optional[Artifact]:
        key = self._get_key(url, "artifacts")
        return self.artifacts.get(key) if key is not None else None

    def get_service(self, url: str) -> Optional[Service]:
        key = self._get_key(url, "services")
        return self.services.get(key) if key is not None else None

    def get_component(self, url: str) -> Optional[Component]:
        key = self._get_key(url, "components")
        return self.components.get(key) if key is not None else None

    def get_instantiation(self, url: str) -> Optional[Instantiation]:
        key = self._get_key(url, "instantiations")
        return self.instantiations.get(key) if key is not None else None

    def get_type(self, name: str) -> Optional[CloudType]:
        key = self._get_key(name, "types")
        return self.types.get(key) if key is not None else None

    def get_record(self, section: str, key: str) -> Optional[Any]:
        """Look up a record by ``(section, key)``.

        Use :py:func:`~unfurl.tosca_plugins.cloudmap_defs.section_of` for the
        section when you have a record rather than a section name.
        """
        return getattr(self, section, {}).get(key)

    def add_record(self, record: CloudMapRecord) -> None:
        if isinstance(record, Repository):
            self.repositories[record.url] = record
        elif isinstance(record, Artifact):
            self.artifacts[record.url] = record
            for art in record.versions.values():
                self.artifacts[art.url] = art
        elif isinstance(record, Service):
            self.services[record.url] = record
            for svc in record.versions.values():
                self.services[svc.url] = svc
        elif isinstance(record, Component):
            self.components[record.url] = record
            for comp in record.versions.values():
                self.components[comp.url] = comp
        elif isinstance(record, Instantiation):
            # Instantiation.__post_init__ auto-generates `url` when not
            # set, so reading record.url is safe here.
            self.instantiations[record.url] = record
            for inst in record.versions.values():
                self.instantiations[inst.url] = inst
        elif isinstance(record, CloudType):
            self.types[record.name] = record
        else:
            raise TypeError(
                f"unsupported cloudmap record type: {type(record).__name__}"
            )

    def delete_record(self, record: CloudMapRecord) -> None:
        """Remove ``record`` (and any version variants) from the
        appropriate section. Missing keys are silently ignored.
        Inverse of :meth:`add_record`.
        """
        if isinstance(record, Repository):
            self.repositories.pop(record.url, None)
        elif isinstance(record, Artifact):
            self.artifacts.pop(record.url, None)
            for art in record.versions.values():
                self.artifacts.pop(art.url, None)
        elif isinstance(record, Service):
            self.services.pop(record.url, None)
            for svc in record.versions.values():
                self.services.pop(svc.url, None)
        elif isinstance(record, Component):
            self.components.pop(record.url, None)
            for comp in record.versions.values():
                self.components.pop(comp.url, None)
        elif isinstance(record, Instantiation):
            self.instantiations.pop(record.url, None)
            for inst in record.versions.values():
                self.instantiations.pop(inst.url, None)
        elif isinstance(record, CloudType):
            self.types.pop(record.name, None)
        else:
            raise TypeError(
                f"unsupported cloudmap record type: {type(record).__name__}"
            )

    def add_image_artifact(self, image: "ContainerImage") -> Artifact:
        """
        Add an OCI artifact to the cloudmap from a container image.

        Creates the artifact, extracts build instantiation, and adds them to the database.

        Args:
            image: ContainerImage to analyze

        Returns:
            Artifact object added to self.artifacts
        """
        artifact, instantiation, artifact_fetch = create_oci_artifact(image)

        # Add instantiation and update artifact.instantiated_by
        if instantiation:
            self.add_record(instantiation)
        # Add artifact to database
        self.add_record(artifact)

        return artifact

    @staticmethod
    def make_empty_cloudmap() -> Dict[str, Any]:
        """Return a new cloudmap document with the default structure."""
        return {
            "apiVersion": API_VERSION,
            "kind": "CloudMap",
            "metadata": {},
            "repositories": {},
            "artifacts": {},
            "services": {},
            "components": {},
            "instantiations": {},
            "types": {},
        }

    def _load(self, path: str, contents=None, validate: bool = True) -> None:
        if contents is None:
            if os.path.isdir(path):
                path = os.path.join(path, self.DEFAULT_NAME)
            contents = self.make_empty_cloudmap()  # if path doesn't exist
            config_path = path
        else:
            # the cloudmap was already loaded, ``path`` only says where from
            # (it might not even be a local file, e.g. a CloudMapCache's url)
            config_path = None
        self.config = YamlConfig(
            contents,
            config_path,
            validate,
            schema=os.path.join(_basepath, "cloudmap-schema.json"),
        )
        # Cloudmap URLs can be very long; prevent ruamel.yaml from using
        # explicit key syntax ("? key\n:") by raising the simple key limit.
        self.config.yaml.emitter.MAX_SIMPLE_KEY_LENGTH = 1024
        # where this cloudmap lives, so references to another one can be
        # distinguished from references to this one (see _matches_cloudmap_url)
        self.path: str = self.config.path or path
        db = self.config.config
        assert isinstance(db, dict)
        self.metadata = cast(Dict[str, Any], db.get("metadata") or {})

        # Load types first (may be augmented during notable migration)
        types = cast(Dict, db.get("types") or {})
        self.types: CloudTypeDict = {
            name: (
                t
                if isinstance(t, CloudType)
                else CloudType(name=t.pop("name", name), **t)
            )
            for name, t in types.items()
        }

        # Load artifacts (may be augmented during notable migration)
        artifacts = cast(Dict, db.get("artifacts") or {})
        self.artifacts: ArtifactDict = self._load_resource(artifacts, Artifact)

        # Load repositories and migrate old notable format
        repositories = cast(Dict, db.get("repositories") or {})
        self.repositories: RepositoryDict = {}
        for url, r in repositories.items():
            # Backwards compatibility: migrate the deprecated `notable` key to `contains`
            old_notable = r.pop("notable", None)
            repo = Repository(url=r.pop("git", url), **r)
            if isinstance(old_notable, dict):
                from .analyzers import migrate_old_notable_format

                migrate_old_notable_format(self, repo, old_notable)
            self.add_record(repo)

        # Load services
        services = cast(Dict, db.get("services") or {})
        self.services: ServiceDict = self._load_resource(services, Service)

        # Load components
        components = cast(Dict, db.get("components") or {})
        self.components: ComponentDict = self._load_resource(components, Component)

        # Load instantiations
        instantiations = cast(Dict, db.get("instantiations") or {})
        self.instantiations: Dict[str, Instantiation] = self._load_resource(
            instantiations, Instantiation
        )

        self.db = cast(Dict[str, Any], db)

    def _load_resource(
        self,
        resources: Dict[str, Any],
        cls: Union[Type[Artifact], Type[Service], Type[Component], Type[Instantiation]],
    ) -> Dict[str, Any]:
        resource_dict = {
            url: (a if isinstance(a, cls) else cls(url=a.pop("url", url), **a))
            for url, a in resources.items()
        }
        # add versions to top level for easy lookup by url#version
        versions = {}
        for url, resource in resource_dict.items():
            for version_key, obj in resource.versions.items():
                versions[obj.url] = obj  # obj.url has been merged with its parent url
        resource_dict.update(versions)
        return resource_dict

    def reload(self):
        assert self.config.path
        self._load(self.config.path)

    def save(self, msg: str = "", commit: bool = True) -> bool:
        """Write the document back to its file. ``msg`` and ``commit`` are
        unused -- committing is the caller's job (see
        :py:meth:`unfurl.cloudmap.CloudMap.save`)."""
        # maintain order of repositories so git merge is effective
        # we want to support mirrors
        changed = False
        old_metadata = self.db.pop("metadata", None)
        if self.metadata:
            new_metadata = {k: self.metadata[k] for k in sorted(self.metadata)}
            if new_metadata != old_metadata:
                changed = True
            self.db["metadata"] = new_metadata
        elif old_metadata:
            changed = True

        old_repositories = self.db.pop("repositories", None)
        new_repositories = {
            k: self.repositories[k].asdict() for k in sorted(self.repositories)
        }
        if new_repositories != old_repositories:
            changed = True
        self.db["repositories"] = new_repositories

        old_artifacts = self.db.pop("artifacts", None)
        if self.artifacts:
            new_artifacts = {
                url: val.asdict()
                for url, val in sorted(self.artifacts.items())
                if not val._parent  # type: ignore[attr-defined]
            }
            if new_artifacts != old_artifacts:
                changed = True
            self.db["artifacts"] = new_artifacts
        elif old_artifacts:
            changed = True

        old_services = self.db.pop("services", None)
        if self.services:
            new_services = {
                url: val.asdict()
                for url, val in sorted(self.services.items())
                if not val._parent  # type: ignore[attr-defined]
            }
            if new_services != old_services:
                changed = True
            self.db["services"] = new_services
        elif old_services:
            changed = True

        old_components = self.db.pop("components", None)
        if self.components:
            new_components = {
                url: val.asdict()
                for url, val in sorted(self.components.items())
                if not val._parent  # type: ignore[attr-defined]
            }
            if new_components != old_components:
                changed = True
            self.db["components"] = new_components
        elif old_components:
            changed = True

        old_instantiations = self.db.pop("instantiations", None)
        if self.instantiations:
            new_instantiations = {
                key: val.asdict()
                for key, val in sorted(self.instantiations.items())
                if not val._parent  # type: ignore[attr-defined]
            }
            if new_instantiations != old_instantiations:
                changed = True
            self.db["instantiations"] = new_instantiations
        elif old_instantiations:
            changed = True

        old_types = self.db.pop("types", None)
        if self.types:
            new_types = {
                name: (
                    self.types[name].asdict()
                    if isinstance(self.types[name], CloudType)
                    else self.types[name]
                )
                for name in sorted(self.types)
            }
            if new_types != old_types:
                changed = True
            self.db["types"] = new_types
        elif old_types:
            changed = True

        self.config.save()
        return changed

    def _subtype_names(self, type: str) -> Set[str]:
        """``type`` plus every type in this document that extends it.

        Matches what the server's ``type=`` query parameter does, so a
        record found locally is a record the server would have returned.
        """
        return subtype_closure(extends_children(self.types), type)

    def find_artifacts(self, type: str = "") -> Iterable[Artifact]:
        """An empty filter returns all artifacts; otherwise ``type`` and its subtypes."""
        if not type:
            return self.artifacts.values()
        names = self._subtype_names(type)
        return (a for a in self.artifacts.values() if not names.isdisjoint(a.type.types))

    def find_services(self, type: str = "") -> Iterable[Service]:
        """An empty filter returns all services; otherwise ``type`` and its subtypes."""
        if not type:
            return self.services.values()
        names = self._subtype_names(type)
        return (s for s in self.services.values() if not names.isdisjoint(s.type.types))

    def find_components(self, type: str = "") -> Iterable[Component]:
        """An empty filter returns all components; otherwise ``type`` and its subtypes."""
        if not type:
            return self.components.values()
        names = self._subtype_names(type)
        return (
            c for c in self.components.values() if not names.isdisjoint(c.type.types)
        )

    def find_instantiations(self, type: str = "") -> Iterable[Instantiation]:
        """An empty filter returns all instantiations; otherwise ``type`` and its subtypes."""
        if not type:
            return self.instantiations.values()
        names = self._subtype_names(type)
        return (
            i for i in self.instantiations.values() if not names.isdisjoint(i.type.types)
        )

    def find_types(self) -> Iterable[CloudType]:
        return self.types.values()

    def find_repositories(self) -> Iterable[Repository]:
        return self.repositories.values()
