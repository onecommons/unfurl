import os
from pathlib import Path
import traceback
from click.testing import CliRunner
import pytest
from unfurl.__main__ import cli
import git
from unfurl.tosca_plugins.cloudmap_defs import (
    Artifact,
    ArtifactMetadata,
    CloudType,
    CommonMetadata,
    Component,
    Discovery,
    EntitySchema,
    Instantiation,
    is_label,
    join_resource_url,
    Repository,
    RepositoryMetadata,
    TypeRefs,
    TypeRefConstraint,
    Service,
)
from unfurl.util import change_cwd, API_VERSION
from unfurl.repo import sanitize_url
from unfurl.testing import init_project, run_cmd, run_job_cmd
from unittest.mock import Mock, patch
from unfurl.cloudmap import (
    CloudMap,
    GitlabManager,
    GithubManager,
    CloudMapDB,
)

# The cloudmap `test_create` expects after syncing with the test provider, and
# the source the graph fixtures are derived from. Rebuild it by running
# `test_create[]` with $UNFURL_TEST_TMPDIR set and passing the resulting
# cloudmap.yaml to this file's __main__ block -- see its docstring.
EXPECTED_CLOUDMAP_FIXTURE = (
    Path(__file__).parent / "fixtures" / "expected_cloudmap.yaml"
)
expected_cloudmap = EXPECTED_CLOUDMAP_FIXTURE.read_text()

def _capture_graph(db: CloudMapDB, start_url: str = "") -> str:
    """Capture cloudmap_graph_console output as plain text (no color/markup)."""
    from unfurl.reporting import cloudmap_graph_console
    from rich.console import Console
    from io import StringIO

    buf = StringIO()
    console = Console(file=buf, force_terminal=False, no_color=True, width=200)
    cloudmap_graph_console(db, start_url, console=console)
    return buf.getvalue()

def main():
    """Update this file in-place.

    Usage:
        python tests/test_cloudmap.py                  # regenerate expected graph outputs
        python tests/test_cloudmap.py cloudmap.yaml    # update expected_cloudmap.yaml from file contents

    To rebuild "expected_cloudmap" run test_create[] with $UNFURL_TEST_TMPDIR set, the cloudmap.yaml path will look like:
    $UNFURL_TEST_TMPDIR/tmpkjk37eir/project/cloudmap/cloudmap.yaml
    """
    import json
    import re
    import sys
    from pathlib import Path

    src = Path(__file__).read_text()

    if len(sys.argv) > 1:
        # `expected_cloudmap` is read from this fixture, so updating it is the
        # whole job -- no source rewriting needed.
        cloudmap_file = Path(sys.argv[1])
        EXPECTED_CLOUDMAP_FIXTURE.write_text(cloudmap_file.read_text())
        print(f"expected_cloudmap.yaml: updated from {cloudmap_file}")
        print("now re-run without arguments to regenerate the graph fixtures")
    else:
        # Regenerate expected graph outputs from expected_cloudmap
        fixture_dir = Path(__file__).parent / "fixtures"
        fixture_dir.mkdir(exist_ok=True)
        cloudmap_fixture = EXPECTED_CLOUDMAP_FIXTURE
        db = CloudMapDB(str(cloudmap_fixture))

        graphs = {
            "expected_full_graph": _capture_graph(db),
            "expected_artifact_graph": _capture_graph(
                db,
                "git://unfurl.cloud/onecommons/blueprints/odoo.git#:ensemble-template.yaml%23spec/service_template",
            ),
            "expected_dual_record_graph": _capture_graph(
                db,
                "git://unfurl.cloud/feb20a/dashboard.git#:environments/aws/onecommons/blueprints/odoo/odoo-aws-1/ensemble.yaml",
            ),
        }

        # Regenerate JSON graph fixtures
        from unfurl.reporting import cloudmap_graph_json

        for name, start_url in [
            ("cloudmap_graph.json", None),
            (
                "cloudmap_graph_artifact.json",
                "git://unfurl.cloud/onecommons/blueprints/odoo.git#:ensemble-template.yaml%23spec/service_template",
            ),
            (
                "cloudmap_graph_dual.json",
                "git://unfurl.cloud/feb20a/dashboard.git#:environments/aws/onecommons/blueprints/odoo/odoo-aws-1/ensemble.yaml",
            ),
        ]:
            result = cloudmap_graph_json(db, start_url)
            fixture_path = fixture_dir / name
            fixture_path.write_text(json.dumps(result, indent=2) + "\n")
            print(f"{name}: updated ({fixture_path})")

        for name, value in graphs.items():
            pattern = rf'({re.escape(name)} = """\\)\n.*?(?=""")'
            replacement = f'{name} = """\\\n{value}'
            src, count = re.subn(pattern, replacement, src, flags=re.DOTALL)
            status = "updated" if count else "NOT FOUND"
            print(f"{name}: {status}")

        Path(__file__).write_text(src)
        print(f"\nUpdated {Path(__file__).name}")


if __name__ == "__main__":
    main()

UNFURL_TEST_CLOUDMAP_URL = os.getenv("UNFURL_TEST_CLOUDMAP_URL")

# Mark to skip integration tests if UNFURL_TEST_CLOUDMAP_URL not set
skip_integration = pytest.mark.skipif(
    not UNFURL_TEST_CLOUDMAP_URL,
    reason="need UNFURL_TEST_CLOUDMAP_URL set to run integration test",
)


@pytest.mark.parametrize(
    "attrs, status",
    [
        ({"archived": False}, None),
        ({"archived": True}, "archived"),
        ({"archived": True, "marked_for_deletion_at": "2026-10-01"}, "deleted"),
        ({"archived": False, "marked_for_deletion_on": "2026-10-01"}, "deleted"),
        ({}, None),
    ],
)
def test_gitlab_project_status(attrs, status):
    import gitlab
    from gitlab.v4.objects import Project
    from unfurl.cloudmap.gitlab import _project_status

    manager = gitlab.Gitlab("https://gitlab.example.com").projects
    assert _project_status(Project(manager, {"id": 1, **attrs})) == status


def test_is_label():
    # labels: word chars, '-', '.'
    assert is_label("web")
    assert is_label("Database")
    assert is_label(".gitlab-ci.yml")  # dotted file names look like labels
    assert is_label("software.Nginx")
    # not labels: urls, paths, global type ids (contain ':' '/' '@')
    assert not is_label("")
    assert not is_label("git://example.com/repo.git")
    assert not is_label("pkg:oci/nginx")
    assert not is_label("ensemble/ensemble.yaml")
    assert not is_label("CronicleApp@unfurl.cloud/onecommons/blueprints/cronicle")


def _schema_properties(node: dict, defs: dict) -> list:
    """Property names of a schema node in declaration order, expanding ``allOf``.

    A ``$ref`` branch contributes the referenced definition's properties at the
    position the branch appears in, which is how ``artifact`` and ``component``
    place the shared ``relationships`` block between their own fields.
    """
    names = list(node.get("properties", {}))
    for branch in node.get("allOf", []):
        if "$ref" in branch and "properties" not in branch:
            branch = defs[branch["$ref"].rsplit("/", 1)[-1]]
        names += [n for n in _schema_properties(branch, defs) if n not in names]
    return names


def test_record_discovery_source():
    """Stamping is idempotent, additive, and merges the record it replaces."""
    from unfurl.cloudmap.provenance import (
        discovery_sources,
        record_discovery_source,
    )

    artifact = Artifact(url="pkg:oci/x")
    assert discovery_sources(artifact) == [], "no discovery metadata yet"

    record_discovery_source(artifact, "https://example.com/a")
    record_discovery_source(artifact, "https://example.com/a")
    assert discovery_sources(artifact) == ["https://example.com/a"], "idempotent"

    record_discovery_source(artifact, "https://example.com/b")
    assert discovery_sources(artifact) == [
        "https://example.com/a",
        "https://example.com/b",
    ], "a record can be discovered from several places"

    # An analyzer that rebuilds a record from scratch (create_oci_artifact
    # assigns a fresh Discovery) must not drop what other analyzers recorded.
    rebuilt = Artifact(
        url="pkg:oci/x",
        metadata=ArtifactMetadata(
            discovery=Discovery(sources=["https://registry.example.com/v2/x"])
        ),
    )
    record_discovery_source(rebuilt, "https://example.com/a", previous=artifact)
    assert discovery_sources(rebuilt) == [
        "https://registry.example.com/v2/x",
        "https://example.com/a",
        "https://example.com/b",
    ]

    # Skipping the source must not skip the merge: an artifact rebuilt from
    # scratch (create_oci_artifact assigns a fresh Discovery) still has to
    # keep what other analyzers recorded, or `--replace` could never find it
    # again.
    readded = Artifact(
        url="pkg:oci/x",
        metadata=ArtifactMetadata(
            discovery=Discovery(sources=["https://registry.example.com/v2/x"])
        ),
    )
    record_discovery_source(readded, "pkg:oci/x", previous=artifact)
    assert discovery_sources(readded) == [
        "https://registry.example.com/v2/x",
        "https://example.com/a",
        "https://example.com/b",
    ], "the record's own url is skipped, the inherited sources are not"

    # A url the record already names isn't worth recording: neither the
    # record itself nor a file inside it (the repository didn't come *from*
    # its own file -- it contains it).
    repository = Repository(url="git://example.com/r.git", path="r")
    record_discovery_source(repository, "git://example.com/r.git#:f.yaml")
    record_discovery_source(repository, "git://example.com/r.git")
    assert discovery_sources(repository) == []
    assert "metadata" not in repository.asdict()

    # metadata is always coerced to the record's own subclass, so stamping
    # works the same on every record type.
    record_discovery_source(repository, "https://example.com/api/r")
    assert isinstance(repository.metadata, RepositoryMetadata)
    assert repository.asdict()["metadata"] == {
        "discovery": {"sources": ["https://example.com/api/r"]}
    }


def test_dataclass_field_order_matches_schema():
    """The dataclasses and the JSON Schema must declare fields in the same order.

    ``CloudMapDB.save()`` writes each record via ``asdict()``, so the dataclass
    order is the order records land in on disk. The Rust writer derives its
    canonical order from the schema instead (``DataFormat::field_order``), so
    the two agreeing is what keeps a record from being rewritten end to end
    every time the other tool touches it.
    """
    import dataclasses
    import json
    from pathlib import Path

    from unfurl.tosca_plugins import cloudmap_defs

    schema = json.loads(
        (
            Path(cloudmap_defs.__file__).parents[1] / "cloudmap" / "cloudmap-schema.json"
        ).read_text()
    )
    defs = schema.get("$defs") or schema["definitions"]

    pairs = [
        (Repository, defs["repository"]),
        (Artifact, defs["artifact"]),
        (Component, defs["component"]),
        (Service, defs["service"]),
        (Instantiation, defs["instantiation"]),
        (CloudType, defs["type"]),
        (CommonMetadata, defs["metadata"]),
        (Discovery, defs["discovery"]),
        # metadata subclasses, defined inline where they're used
        (ArtifactMetadata, defs["artifact"]["allOf"][2]["properties"]["metadata"]),
        (RepositoryMetadata, defs["repository"]["properties"]["metadata"]),
    ]
    for dataclass, node in pairs:
        # `url` is the record's key, never written into the value.
        fields = [f.name for f in dataclasses.fields(dataclass) if f.name != "url"]
        assert fields == _schema_properties(node, defs), dataclass.__name__


def test_typed_urls_fromdict_and_asdict():
    tr = TypeRefs({"software.Nginx": {"version": "1.25"}})

    # 1. plain url form: url -> ("", url)
    d = TypeRefs.urls_fromdict({"git://example.com/repo.git": tr})
    assert d == {("", "git://example.com/repo.git"): tr}
    assert TypeRefs.urls_asdict(d) == {"git://example.com/repo.git": tr.asdict()}

    # 2. label with a plain typeRefs map (keys are type names) -> (label, "")
    d = TypeRefs.urls_fromdict({"Database": {"PostgresDB@ns/x": None}})
    assert d == {("Database", ""): TypeRefs({"PostgresDB@ns/x": None})}
    assert TypeRefs.urls_asdict(d) == {"Database": {"PostgresDB@ns/x": None}}

    # 3. label with a nested {url: typeRefs} map -> (label, url) per nested url
    d = TypeRefs.urls_fromdict({"the_db": {"git://example.com/db.git": None}})
    assert d == {("the_db", "git://example.com/db.git"): TypeRefs(None)}
    assert TypeRefs.urls_asdict(d) == {"the_db": {"git://example.com/db.git": None}}

    # 4. keys_are_urls=True: skip is_label, dotted file names stay url-parts
    d = TypeRefs.urls_fromdict(
        {".gitlab-ci.yml": {"cloudmap.artifacts.GitLabPipeline": None}},
        keys_are_urls=True,
    )
    assert d == {
        ("", ".gitlab-ci.yml"): TypeRefs({"cloudmap.artifacts.GitLabPipeline": None})
    }

    # 5. already-normalized (tuple keys) pass through unchanged
    tuple_keyed = {("lbl", "git://example.com/x.git"): tr}
    assert TypeRefs.urls_fromdict(tuple_keyed) == tuple_keyed


def test_normalize_url():
    # _normalize_url returns (cloudmap_url, key, path)
    normalize = CloudMapDB._normalize_url

    # json pointer fragment form
    assert normalize("#/services/https:~1~1example.com~1", "services") == (
        "",
        "https://example.com/",
        [],
    )
    assert normalize("#/artifacts/foo", "services") == ("", "#/artifacts/foo", [])
    # the keys after the record key are a path into the record
    assert normalize("#/services/blah/blah", "services") == ("", "blah", ["blah"])
    assert normalize("#/services/blah/a~1b/c", "services") == (
        "",
        "blah",
        ["a/b", "c"],
    )

    # opaque-key shorthand: first key has a ":" or "@" so the rest is the key
    assert normalize("service:https://example.com/", "services") == (
        "",
        "https://example.com/",
        [],
    )
    assert normalize("instantiation:https:foo.com/path#version", "instantiations") == (
        "",
        "https:foo.com/path#version",
        [],
    )
    assert normalize("type:Odoo@unfurl.cloud/onecommons/blueprints/odoo", "types") == (
        "",
        "Odoo@unfurl.cloud/onecommons/blueprints/odoo",
        [],
    )
    assert normalize("artifact:pkg:oci:docker.io/library/nginx", "artifacts") == (
        "",
        "pkg:oci:docker.io/library/nginx",
        [],
    )
    # percent-encoding in an opaque key is significant, don't decode it
    assert normalize(
        "artifact:git://example.com/foo.git#:ensemble-template.yaml%23spec/service_template",
        "artifacts",
    ) == (
        "",
        "git://example.com/foo.git#:ensemble-template.yaml%23spec/service_template",
        [],
    )

    # undelimited keys are percent-decoded
    assert normalize("component:Database", "components") == ("", "Database", [])
    assert normalize("component:a%2Fb", "components") == ("", "a/b", [])

    # delimited keys are used verbatim
    assert normalize("service:[https://x]", "services") == ("", "https://x", [])
    assert normalize("service:[https://x]/path", "services") == (
        "",
        "https://x",
        ["path"],
    )
    assert normalize("instantiation:foo/instantiated", "instantiations") == (
        "",
        "foo",
        ["instantiated"],
    )
    # nested brackets: the closing "]" is the one matching the opening "["
    assert normalize("component:[instantiation:[https://x]/foo]", "components") == (
        "",
        "instantiation:[https://x]/foo",
        [],
    )
    # the two equivalent forms in the schema reference resolve the same way
    expected = (
        "",
        "https://pipeline.com/myrepo/34",
        ["instantiated", "component:mycomponent@example.org:foo"],
    )
    assert (
        normalize(
            "#/instantiations/https:~1~1pipeline.com~1myrepo~134/instantiated/component:mycomponent@example.org:foo",
            "instantiations",
        )
        == expected
    )
    assert (
        normalize(
            "instantiation:[https://pipeline.com/myrepo/34]/instantiated/[component:mycomponent@example.org:foo]",
            "instantiations",
        )
        == expected
    )

    # the cloudmap document containing the record can be named explicitly
    assert normalize("cloudmap:[file:cm.yaml]:service:[https://x]", "services") == (
        "file:cm.yaml",
        "https://x",
        [],
    )
    assert normalize(
        "cloudmap:[git://github.com/cloudmap.git#:cloudmap.yaml]:service:https://x/1.2",
        "services",
    ) == ("git://github.com/cloudmap.git#:cloudmap.yaml", "https://x/1.2", [])
    # omitting it means this cloudmap
    assert normalize("cloudmap:service:[https://x]/path", "services") == (
        "",
        "https://x",
        ["path"],
    )

    # record type must match the section we're looking in
    assert normalize("artifact:x@y", "services") == ("", "artifact:x@y", [])
    assert normalize("bogus:x@y", "services") == ("", "bogus:x@y", [])
    assert normalize("cloudmap:[file:cm.yaml]:artifact:x@y", "services") == (
        "",
        "cloudmap:[file:cm.yaml]:artifact:x@y",
        [],
    )
    # not a pseudo-url at all
    assert normalize("pkg:oci:docker.io/library/nginx", "artifacts") == (
        "",
        "pkg:oci:docker.io/library/nginx",
        [],
    )
    assert normalize("https://example.com/", "services") == (
        "",
        "https://example.com/",
        [],
    )
    assert normalize("service:", "services") == ("", "service:", [])

    # malformed references are left alone
    for malformed in (
        "service:[https://x",  # missing the matching "]"
        "service:[https://x]junk",  # trailing characters after a delimited key
        "service:[https://x]/",  # trailing "/"
        "service:foo//bar",  # empty key
        "service:[]",  # empty delimited key
        "service:fo[o",  # undelimited bracket
        "cloudmap:[file:cm.yaml:service:x",  # unterminated cloudmap url
        "cloudmap:[file:cm.yaml]service:x",  # missing ":" after the cloudmap url
        "cloudmap:[]:service:x",  # empty cloudmap url
    ):
        assert normalize(malformed, "services") == ("", malformed, [])


def test_get_record_by_pseudo_url(tmp_path):
    cloudmap_path = tmp_path / "cloudmap.yaml"
    cloudmap_path.write_text(expected_cloudmap)
    db = CloudMapDB(str(cloudmap_path))

    repo_url = "git://unfurl.cloud/feb20a/dashboard.git"
    assert db.get_repository("repository:" + repo_url) is db.repositories[repo_url]

    artifact_url = "git://unfurl.cloud/onecommons/blueprints/odoo.git#:ensemble-template.yaml%23spec/service_template"
    assert db.get_artifact("artifact:" + artifact_url) is db.artifacts[artifact_url]

    service_url = "https://example.com/oodo"
    assert db.get_service("service:" + service_url) is db.services[service_url]

    inst_url = "git://unfurl.cloud/feb20a/dashboard.git#:environments/aws/onecommons/blueprints/odoo/odoo-aws-1/ensemble.yaml"
    assert (
        db.get_instantiation("instantiation:" + inst_url) is db.instantiations[inst_url]
    )

    type_name = "Odoo@unfurl.cloud/onecommons/blueprints/odoo"
    assert db.get_type("type:" + type_name) is db.types[type_name]
    # a plain (label-like) key
    relationship = "unfurl.relationships.ConnectsTo.AWSAccount"
    assert db.get_type("type:" + relationship) is db.types[relationship]
    # unprefixed keys still work
    assert db.get_type(type_name) is db.types[type_name]
    assert db.get_service(service_url) is db.services[service_url]
    # wrong record type doesn't resolve
    assert db.get_service("artifact:" + service_url) is None
    # a key path into the record is ignored, the record itself is returned
    assert (
        db.get_service(f"service:[{service_url}]/endpoints") is db.services[service_url]
    )


def test_get_record_by_cloudmap_url(tmp_path):
    cloudmap_path = tmp_path / "cloudmap.yaml"
    cloudmap_path.write_text(expected_cloudmap)
    db = CloudMapDB(str(cloudmap_path))
    assert db.path == str(cloudmap_path)

    service_url = "https://example.com/oodo"
    service = db.services[service_url]
    type_name = "Odoo@unfurl.cloud/onecommons/blueprints/odoo"

    def get_service(cloudmap_url: str):
        return db.get_service(f"cloudmap:[{cloudmap_url}]:service:{service_url}")

    # this cloudmap, named as a path or a file: url, absolute or relative to it
    assert get_service(str(cloudmap_path)) is service
    assert get_service("file:" + str(cloudmap_path)) is service
    assert get_service(cloudmap_path.as_uri()) is service
    assert get_service("cloudmap.yaml") is service
    assert get_service("file:cloudmap.yaml") is service
    assert get_service("file:./cloudmap.yaml") is service
    assert get_service(f"file:../{tmp_path.name}/cloudmap.yaml") is service
    # the directory containing it
    assert get_service(str(tmp_path)) is service
    assert get_service("file:.") is service
    assert (
        db.get_type(f"cloudmap:[{cloudmap_path}]:type:{type_name}")
        is db.types[type_name]
    )

    # another cloudmap document
    assert get_service("file:other.yaml") is None
    assert get_service(str(tmp_path / "sub" / "cloudmap.yaml")) is None
    assert get_service("git://github.com/cloudmap.git#:cloudmap.yaml") is None
    assert get_service("https://example.com/cloudmap.yaml") is None
    # a bare fragment or query isn't a document url
    assert get_service("#foo") is None
    assert get_service("?q") is None
    assert db.get_type(f"cloudmap:[file:other.yaml]:type:{type_name}") is None

    # a "file:" url percent-encodes what a bare path spells out
    spaced_path = tmp_path / "my map.yaml"
    spaced_path.write_text(expected_cloudmap)
    spaced = CloudMapDB(str(spaced_path))
    for cloudmap_url in (spaced_path.as_uri(), "file:my%20map.yaml", "my map.yaml"):
        assert (
            spaced.get_service(f"cloudmap:[{cloudmap_url}]:service:{service_url}")
            is not None
        )

    # a cloudmap that wasn't loaded from a file only matches the empty url
    in_memory = CloudMapDB("", contents=db.db, validate=False)
    assert not in_memory.path
    assert in_memory.get_service(f"service:{service_url}") is not None
    assert (
        in_memory.get_service(f"cloudmap:[cloudmap.yaml]:service:{service_url}") is None
    )

    # given contents, the path is just where the cloudmap came from -- it can
    # be relative (e.g. a path in a repository, as the server passes) and the
    # file it names is never read
    with change_cwd(str(tmp_path)):
        preloaded = CloudMapDB("cloudmap.yaml", contents=db.db, validate=False)
        assert preloaded.path == "cloudmap.yaml"
        for cloudmap_url in ("cloudmap.yaml", "file:cloudmap.yaml", "file:."):
            ref = f"cloudmap:[{cloudmap_url}]:service:{service_url}"
            assert preloaded.get_service(ref) is not None
        ref = f"cloudmap:[other.yaml]:service:{service_url}"
        assert preloaded.get_service(ref) is None
        # the file at that path exists but isn't loaded
        empty = CloudMapDB("cloudmap.yaml", contents=CloudMapDB.make_empty_cloudmap())
        assert not empty.services and not empty.repositories


# XXX more tests:
# add readonly public test of --import (doesn't need UNFURL_TEST_CLOUDMAP_URL)
# add local test: unfurl cloudmap --sync local --clone-root local-repos
# add commit in local repo and add a project to upstream cloudmap
# verify that sync updates testProvider properly (and delete the created project)

unfurl_yaml = """
apiVersion: unfurl/v1alpha1
kind: Project
environments:
  defaults:
    repositories:
      cloudmap:
        # this is needed for the configurator
        # we need the "#:." make it clear this is a git url
        url: file:../cloudmap#:.
    cloudmaps:
      repositories:
        cloudmap:
          # the other tests need a regular file url, luckily we can set url here too
          url: file:../cloudmap
          clone_root: ../repos
      hosts:
        testProvider:
          type: gitlab
          url:
            get_env: UNFURL_TEST_CLOUDMAP_URL
          canonical_url: https://unfurl.cloud
"""

ensemble_yaml = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
spec:
  service_template:
    node_types:
      CloudMapExporter:
        derived_from: tosca.nodes.Root
        interfaces:
          Standard:
            operations:
              configure:
                implementation: CloudMap
                inputs:
                  host:
                    url:
                      get_env: UNFURL_TEST_CLOUDMAP_URL
                  cloudmap: cloudmap
                  namespace: feb20a

    topology_template:
      node_templates:
        cloudmap_exporter:
          type: CloudMapExporter
"""

SAVE_TMP = os.getenv("UNFURL_TEST_TMPDIR")

@pytest.fixture(scope="module")
def runner():
    runner = CliRunner()
    with runner.isolated_filesystem(SAVE_TMP) as test_dir:
        if SAVE_TMP:
            print("saving to", test_dir)
        os.system("git init cloudmap")
        with change_cwd("cloudmap"):
            with open("README", "w") as foo:
                foo.write("empty")
            os.system("git add README")
            os.system("git commit -m'initial commit'")
            # switch branches so we can push to main later
            os.system("git checkout -b ignore")

        os.makedirs("repos")
        init_project(
            runner,
            args=["init", "--mono", "--var", "vaultid", "", "project"],
            env=dict(UNFURL_HOME=""),
        )
        with change_cwd("project"):
            # Create a mock deployment
            with open("unfurl.yaml", "w") as f:
                f.write(unfurl_yaml)

            yield runner


@skip_integration
@pytest.mark.parametrize("commit", ["", "--commit"])
def test_create(runner: CliRunner, caplog, commit: str):
    run_cmd(
        runner,
        ["--home", ""]
        + f"cloudmap {commit} --sync testProvider --namespace feb20a".split(),
        print_result=True,
    )
    with change_cwd("cloudmap"):
        with open("cloudmap.yaml") as f:
            cloudmap = f.read().rstrip()
            assert cloudmap == expected_cloudmap.rstrip()
        assert not os.system("git push origin main")

    assert "importing group feb20a" in caplog.text
    assert "importing group feb20a/feb20b" in caplog.text
    assert "syncing to feb20a" in caplog.text
    if commit:
        assert (
            "committed: Update hosts/testProvider with latest from testProvider/feb20a"
            in caplog.text
        )
        assert 'nothing to commit for "synced to testProvider"' in caplog.text


@skip_integration
def test_sync(runner, caplog):
    # run again, should be a no op
    run_cmd(
        runner,
        ["--home", ""]
        + "cloudmap --sync testProvider --commit --namespace feb20a".split(),
    )
    assert UNFURL_TEST_CLOUDMAP_URL
    for msg in [
        "found git repo git://unfurl.cloud/feb20a/dashboard.git",
        'nothing to commit for "Update hosts/testProvider with latest from testProvider/feb20a"',
        "syncing to feb20a",
        f"skipping push: no change detected on branch testProvider/main for {sanitize_url(UNFURL_TEST_CLOUDMAP_URL)}/feb20a/dashboard.git",
        'nothing to commit for "synced to testProvider"',
    ]:
        assert msg in caplog.text


@skip_integration
def test_configurator(runner, caplog):
    """Test using CloudMapConfigurator from an ensemble"""
    assert UNFURL_TEST_CLOUDMAP_URL
    # run configurator (exports to cloud)
    with open("ensemble.yaml", "w") as f:
        f.write(ensemble_yaml)

    result, job, summary = run_job_cmd(
        runner,
        ["--home", "", "deploy", "ensemble.yaml"],
    )
    expected = {
        "job": {
            "id": "A01110000000",
            "status": "ok",
            "total": 1,
            "ok": 1,
            "error": 0,
            "unknown": 0,
            "skipped": 0,
            "changed": 1,
        },
        "outputs": {},
        "tasks": [
            {
                "status": "ok",
                "target": "cloudmap_exporter",
                "operation": "configure",
                "template": "cloudmap_exporter",
                "type": "CloudMapExporter",
                "targetStatus": "ok",
                "targetState": "configured",
                "changed": True,
                "configurator": "unfurl.cloudmap.CloudMapConfigurator",
                "priority": "required",
                "reason": "add",
            }
        ],
    }
    assert summary == expected
    result, job, summary = run_job_cmd(
        runner,
        ["--home", "", "deploy", "ensemble.yaml"],
    )
    # no change
    expected["job"]["id"] = "A01110GC0000"
    expected["job"]["changed"] = 0
    expected["job"]["ok"] = 0
    expected["job"]["skipped"] = 1
    expected["tasks"][0]["reason"] = "reconfigure"
    expected["tasks"][0]["status"] = None
    expected["tasks"][0]["changed"] = False
    assert summary == expected


expected_types_cloudmap = f"""apiVersion: {API_VERSION}
kind: CloudMap
repositories:
  git://unfurl.cloud/onecommons/blueprints/cronicle.git:
    path: onecommons/blueprints/cronicle
    name: Cronicle
    protocols:
    - https
    - ssh
    internal_id: '504'
    project_url: https://unfurl.cloud/onecommons/blueprints/cronicle
    metadata:
      description: A simple, distributed task scheduler and runner with a web based
        UI.
      issues_url: https://unfurl.cloud/onecommons/blueprints/cronicle/-/issues
      homepage_url: https://unfurl.cloud/onecommons/blueprints/cronicle
      thumbnail_url: https://unfurl.cloud/onecommons/blueprints/cronicle/-/avatar
    default_branch: main
    branches:
      main: c927e49f0fa1bc6c957cc16ca9d554b46d1abe73
    tags:
      v1.0.0: c927e49f0fa1bc6c957cc16ca9d554b46d1abe73
      v0.1.0: 2f9288e491d47ab0d976c135a5a17475bc9c746a
    contains:
      ensemble-template.yaml#spec/service_template:
        {EntitySchema.CloudBlueprint}:
artifacts:
  git://unfurl.cloud/onecommons/blueprints/cronicle.git#:ensemble-template.yaml%23spec/service_template:
    type:
      {EntitySchema.CloudBlueprint}:
    contains:
      pkg:oci/cronicle?repository_url=docker.io/soulteary:
    instantiates:
      cronicle:
        CronicleApp@unfurl.cloud/onecommons/blueprints/cronicle:
    metadata:
      description: A simple, distributed task scheduler and runner with a web based
        UI.
      title: Cronicle
      version: '0.1'
      thumbnail_url: https://unfurl.cloud/onecommons/blueprints/cronicle/-/avatar
types:
  CronicleApp@unfurl.cloud/onecommons/blueprints/cronicle:
    name: CronicleApp@unfurl.cloud/onecommons/blueprints/cronicle
    kind: component
    metadata:
      title: CronicleApp
      discussion_url: https://unfurl.cloud/onecommons/blueprints/cronicle/-/issues/1
    extends:
    - CronicleApp@unfurl.cloud/onecommons/blueprints/cronicle
    - unfurl.nodes.WebApp@unfurl.cloud/onecommons/std:generic_types
    - WebApp@unfurl.cloud/onecommons/std:generic_types
    - _ContainerAppBase@unfurl.cloud/onecommons/std:generic_types
    - App@unfurl.cloud/onecommons/std:generic_types
    - tosca.nodes.Root
    - tosca.capabilities.Node
    - tosca.capabilities.Root
"""


@pytest.mark.skipif(GithubManager is None, reason="PyGithub not installed")
class TestGithubManager:
    """Unit tests for GithubManager using mock GitHub API objects."""

    @patch("unfurl.cloudmap.github.Github")
    @patch("unfurl.cloudmap.github.Auth")
    def test_init_github_com(self, mock_auth, mock_github_class):
        """Test GithubManager initialization with github.com."""
        config = {
            "type": "github",
            "url": "https://github.com",
            "password": "test_token_123",
        }

        manager = GithubManager("test_github", config)

        assert manager.name == "test_github"
        assert manager.hostname == "github.com"
        assert manager.token == "test_token_123"
        assert manager.base_url == "https://github.com"
        mock_auth.Token.assert_called_once_with("test_token_123")
        mock_github_class.assert_called_once()

    @patch("unfurl.cloudmap.github.Github")
    @patch("unfurl.cloudmap.github.Auth")
    def test_init_github_enterprise(self, mock_auth, mock_github_class):
        """Test GithubManager initialization with GitHub Enterprise."""
        config = {
            "type": "github",
            "url": "https://github.company.com",
            "password": "enterprise_token",
        }

        manager = GithubManager("test_enterprise", config)

        assert manager.hostname == "github.company.com"
        assert manager.base_url == "https://github.company.com"
        # Verify Enterprise API endpoint was used
        call_kwargs = mock_github_class.call_args[1]
        assert call_kwargs["base_url"] == "https://github.company.com/api/v3"

    @patch("unfurl.cloudmap.github.Github")
    @patch("unfurl.cloudmap.github.Auth")
    def test_has_repository_github_url(self, mock_auth, mock_github_class):
        """Test has_repository identifies GitHub repos correctly."""
        config = {"type": "github", "url": "https://github.com", "password": "token"}
        manager = GithubManager("test", config)

        repo = Repository(
            name="test-repo",
            url="git://github.com/user/test-repo.git",
            path="user/test-repo",
            initial_revision="",
            protocols=["https"],
            default_branch="main",
        )

        assert manager.has_repository(repo)

        repo2 = Repository(
            name="test-repo",
            url="git://gitlab.com/user/test-repo.git",
            path="user/test-repo",
            initial_revision="",
            protocols=["https"],
            default_branch="main",
        )

        assert not manager.has_repository(repo2)

    @patch("unfurl.cloudmap.github.Github")
    @patch("unfurl.cloudmap.github.Auth")
    def test_github_repository_to_repository(self, mock_auth, mock_github_class):
        """Test conversion from PyGithub Repository to cloudmap Repository."""
        config = {"type": "github", "url": "https://github.com", "password": "token"}
        manager = GithubManager("test", config)

        # Mock PyGithub Repository object
        mock_repo = Mock()
        mock_repo.name = "test-repo"
        mock_repo.full_name = "testuser/test-repo"
        mock_repo.description = "Test repository"
        mock_repo.private = False
        mock_repo.archived = False
        mock_repo.clone_url = "https://github.com/testuser/test-repo.git"
        mock_repo.ssh_url = "git@github.com:testuser/test-repo.git"
        mock_repo.html_url = "https://github.com/testuser/test-repo"
        mock_repo.default_branch = "main"
        mock_repo.homepage = "https://example.com"
        mock_repo.get_topics.return_value = ["python", "testing"]
        mock_repo.id = 12345

        # Mock license
        mock_license = Mock()
        mock_license.spdx_id = "MIT"
        mock_repo.license = mock_license

        # Mock owner
        mock_owner = Mock()
        mock_owner.login = "testuser"
        mock_repo.owner = mock_owner
        mock_repo.has_issues = True

        # Mock branches
        mock_branch_main = Mock()
        mock_branch_main.name = "main"
        mock_branch_main.commit = Mock()
        mock_branch_main.commit.sha = "abc123"

        mock_branch_dev = Mock()
        mock_branch_dev.name = "develop"
        mock_branch_dev.commit = Mock()
        mock_branch_dev.commit.sha = "def456"

        mock_repo.get_branches.return_value = [mock_branch_main, mock_branch_dev]

        # Mock tags
        mock_tag_v1 = Mock()
        mock_tag_v1.name = "v1.0.0"
        mock_tag_v1.commit = Mock()
        mock_tag_v1.commit.sha = "tag123"

        mock_tag_v2 = Mock()
        mock_tag_v2.name = "v2.0.0"
        mock_tag_v2.commit = Mock()
        mock_tag_v2.commit.sha = "tag456"

        mock_repo.get_tags.return_value = [mock_tag_v1, mock_tag_v2]

        # Convert to cloudmap Repository
        result = manager.github_repository_to_repository(mock_repo)

        assert result.name == "test-repo"
        assert result.url == "git://github.com/testuser/test-repo.git"
        assert result.path == "testuser/test-repo"
        assert result.private is False
        assert result.status is None
        assert result.default_branch == "main"
        assert result.metadata.description == "Test repository"
        assert result.metadata.topics == ["python", "testing"]
        assert result.metadata.spdx_licenses == "MIT"
        assert result.metadata.homepage_url == "https://example.com"
        assert (
            result.metadata.issues_url == "https://github.com/testuser/test-repo/issues"
        )
        # Verify branches and tags
        assert result.branches == {"main": "abc123", "develop": "def456"}
        assert result.tags == {"v1.0.0": "tag123", "v2.0.0": "tag456"}

    @patch("unfurl.cloudmap.github.Github")
    @patch("unfurl.cloudmap.github.Auth")
    def test_get_owner_user(self, mock_auth, mock_github_class):
        """Test get_owner returns authenticated user."""
        config = {"type": "github", "url": "https://github.com", "password": "token"}

        # Mock authenticated user
        mock_user = Mock()
        mock_user.login = "testuser"
        mock_github_instance = mock_github_class.return_value
        mock_github_instance.get_user.return_value = mock_user

        manager = GithubManager("test", config, namespace="")

        result = manager.get_owner("")

        assert result == mock_user
        mock_github_instance.get_user.assert_called_once()

    @patch("unfurl.cloudmap.github.Github")
    @patch("unfurl.cloudmap.github.Auth")
    def test_get_owner_organization(self, mock_auth, mock_github_class):
        """Test get_owner returns organization."""
        config = {"type": "github", "url": "https://github.com", "password": "token"}

        # Mock organization
        mock_org = Mock()
        mock_org.login = "testorg"
        mock_github_instance = mock_github_class.return_value
        mock_github_instance.get_organization.return_value = mock_org

        manager = GithubManager("test", config, namespace="testorg")

        result = manager.get_owner("testorg")

        assert result == mock_org
        mock_github_instance.get_organization.assert_called_once_with("testorg")

    @patch("unfurl.cloudmap.github.Github")
    @patch("unfurl.cloudmap.github.Auth")
    def test_get_owner_org_not_found(self, mock_auth, mock_github_class):
        """Test get_owner raises error when organization not found."""
        config = {"type": "github", "url": "https://github.com", "password": "token"}

        # Mock GithubException for 404
        from github import GithubException

        mock_exception = GithubException(404, {"message": "Not found"}, None)
        mock_github_instance = mock_github_class.return_value
        mock_github_instance.get_organization.side_effect = mock_exception
        mock_github_instance.get_user.side_effect = mock_exception

        manager = GithubManager("test", config)
        assert manager.get_owner("nonexistent") is None

    @patch("unfurl.cloudmap.github.Github")
    @patch("unfurl.cloudmap.github.Auth")
    def test_github_repository_to_repository_conversion(
        self, mock_auth, mock_github_class
    ):
        """Test converting PyGithub Repository to cloudmap Repository with all features."""
        config = {
            "type": "github",
            "url": "https://github.com",
            "password": "token",
            "save_internal": True,
        }
        manager = GithubManager("test", config)

        # Mock PyGithub Repository with all features
        mock_owner = Mock()
        mock_owner.login = "testuser"

        mock_license = Mock()
        mock_license.spdx_id = "MIT"

        mock_repo = Mock()
        mock_repo.name = "test-repo"
        mock_repo.full_name = "testuser/test-repo"
        mock_repo.description = "Test repository with all features"
        mock_repo.private = True
        mock_repo.archived = True
        mock_repo.clone_url = "https://github.com/testuser/test-repo.git"
        mock_repo.ssh_url = "git@github.com:testuser/test-repo.git"
        mock_repo.html_url = "https://github.com/testuser/test-repo"
        mock_repo.default_branch = "develop"
        mock_repo.homepage = "https://example.com"
        mock_repo.get_topics.return_value = ["python", "testing"]
        mock_repo.license = mock_license
        mock_owner.avatar_url = "https://avatars.githubusercontent.com/u/12345"
        mock_repo.owner = mock_owner
        mock_repo.has_issues = True
        mock_repo.id = 12345

        # Mock multiple branches
        mock_branch_main = Mock()
        mock_branch_main.name = "main"
        mock_branch_main.commit = Mock()
        mock_branch_main.commit.sha = "abc123def456"

        mock_branch_dev = Mock()
        mock_branch_dev.name = "develop"
        mock_branch_dev.commit = Mock()
        mock_branch_dev.commit.sha = "789ghi012jkl"

        mock_repo.get_branches.return_value = [mock_branch_main, mock_branch_dev]

        # Mock multiple tags
        mock_tag_v1 = Mock()
        mock_tag_v1.name = "v1.0.0"
        mock_tag_v1.commit = Mock()
        mock_tag_v1.commit.sha = "tag111aaa222"

        mock_tag_v2 = Mock()
        mock_tag_v2.name = "v2.0.0"
        mock_tag_v2.commit = Mock()
        mock_tag_v2.commit.sha = "tag333bbb444"

        mock_repo.get_tags.return_value = [mock_tag_v1, mock_tag_v2]

        # Convert to cloudmap Repository
        result = manager.github_repository_to_repository(mock_repo)

        # Verify basic properties
        assert result.name == "test-repo"
        assert result.url == "git://github.com/testuser/test-repo.git"
        assert result.path == "testuser/test-repo"
        assert result.private is True
        assert result.status == "archived"
        assert result.default_branch == "develop"
        assert result.metadata.description == "Test repository with all features"
        assert result.metadata.topics == ["python", "testing"]
        assert result.metadata.homepage_url == "https://example.com"
        assert result.metadata.spdx_licenses == "MIT"

        # Verify internal_id is saved when save_internal=True
        assert result.internal_id == "12345"

        # Verify branches were correctly extracted
        assert result.branches == {"main": "abc123def456", "develop": "789ghi012jkl"}

        # Verify tags were correctly extracted
        assert result.tags == {"v1.0.0": "tag111aaa222", "v2.0.0": "tag333bbb444"}

    @patch("unfurl.cloudmap.github.Github")
    @patch("unfurl.cloudmap.github.Auth")
    def test_create_project_organization(self, mock_auth, mock_github_class):
        """Test creating a repository in an organization."""
        config = {"type": "github", "url": "https://github.com", "password": "token"}
        manager = GithubManager("test", config)
        manager.dryrun = False

        # Mock organization
        mock_org = Mock()
        mock_org.login = "testorg"

        # Mock created repo with all required attributes
        mock_created_repo = Mock()
        mock_created_repo.full_name = "testorg/new-repo"
        mock_created_repo.description = "New repository"
        mock_created_repo.private = True
        mock_created_repo.get_topics.return_value = []  # Return list for get_topics
        mock_org.create_repo.return_value = mock_created_repo

        # Mock repo to create
        repo_info = Repository(
            name="new-repo",
            url="git://github.com/testorg/new-repo.git",
            path="testorg/new-repo",
            initial_revision="",
            protocols=["https"],
            default_branch="main",
            metadata=RepositoryMetadata(
                description="New repository",
                topics=["new"],
            ),
            private=True,
        )

        result = manager.create_project(repo_info, mock_org)

        assert result == mock_created_repo
        mock_org.create_repo.assert_called_once_with(
            name="new-repo",
            description="New repository",
            private=True,
            auto_init=False,
        )

    @patch("unfurl.cloudmap.github.Github")
    @patch("unfurl.cloudmap.github.Auth")
    def test_create_project_user(self, mock_auth, mock_github_class):
        """Test creating a repository for authenticated user."""
        config = {"type": "github", "url": "https://github.com", "password": "token"}

        # Mock authenticated user
        mock_user = Mock()
        mock_user.login = "testuser"

        # Mock created repo with all required attributes
        mock_created_repo = Mock()
        mock_created_repo.full_name = "testuser/user-repo"
        mock_created_repo.description = "User repository"
        mock_created_repo.private = False
        mock_created_repo.get_topics.return_value = []  # Return list for get_topics
        mock_user.create_repo.return_value = mock_created_repo

        manager = GithubManager("test", config)
        manager.dryrun = False

        # Mock repo to create
        repo_info = Repository(
            name="user-repo",
            url="git://github.com/testuser/user-repo.git",
            path="testuser/user-repo",
            initial_revision="",
            protocols=["https"],
            default_branch="main",
            metadata=RepositoryMetadata(
                description="User repository",
                topics=[],
            ),
            private=False,
        )

        result = manager.create_project(repo_info, mock_user)

        assert result == mock_created_repo
        mock_user.create_repo.assert_called_once_with(
            name="user-repo",
            description="User repository",
            private=False,
            auto_init=False,
        )

    @patch("unfurl.cloudmap.github.Github")
    @patch("unfurl.cloudmap.github.Auth")
    def test_create_project_private_unset_is_public(self, mock_auth, mock_github_class):
        """A record only records ``private`` when it's true, so unset is public."""
        config = {"type": "github", "url": "https://github.com", "password": "token"}
        mock_user = Mock()
        mock_user.login = "testuser"
        mock_user.create_repo.return_value = Mock(get_topics=Mock(return_value=[]))
        manager = GithubManager("test", config)
        manager.dryrun = False
        repo_info = Repository(
            name="user-repo",
            url="git://github.com/testuser/user-repo.git",
            path="testuser/user-repo",
        )
        manager.create_project(repo_info, mock_user)
        assert mock_user.create_repo.call_args.kwargs["private"] is False

    @patch("unfurl.cloudmap.github.Github")
    @patch("unfurl.cloudmap.github.Auth")
    def test_update_project_metadata(self, mock_auth, mock_github_class):
        """Test updating repository metadata."""
        config = {"type": "github", "url": "https://github.com", "password": "token"}
        manager = GithubManager("test", config)
        manager.dryrun = False

        # Mock existing repo
        mock_repo = Mock()
        mock_repo.full_name = "testuser/test-repo"
        mock_repo.description = "Old description"
        mock_repo.private = True
        mock_repo.get_topics.return_value = ["old"]

        # New metadata
        repo_info = Repository(
            name="test-repo",
            url="git://github.com/testuser/test-repo.git",
            path="testuser/test-repo",
            initial_revision="",
            protocols=["https"],
            default_branch="main",
            metadata=RepositoryMetadata(
                description="New description",
                topics=["new", "updated"],
            ),
            private=False,
        )

        result = manager.update_project_metadata(repo_info, mock_repo)

        assert result is True
        mock_repo.edit.assert_called()  # Called for description and visibility
        mock_repo.replace_topics.assert_called_once_with(["new", "updated"])

    @patch("unfurl.cloudmap.github.Github")
    @patch("unfurl.cloudmap.github.Auth")
    def test_git_url_with_auth(self, mock_auth, mock_github_class):
        """Test generating authenticated git URL."""
        config = {
            "type": "github",
            "url": "https://github.com",
            "password": "secret_token",
        }
        manager = GithubManager("test", config)

        mock_repo = Mock()
        mock_repo.clone_url = "https://github.com/user/repo.git"

        result = manager.git_url_with_auth(mock_repo)

        assert result == "https://secret_token@github.com/user/repo.git"

    @patch("unfurl.cloudmap.github.Github")
    @patch("unfurl.cloudmap.github.Auth")
    def test_canonize_with_canonical_url(self, mock_auth, mock_github_class):
        """Test URL canonization with canonical URL set."""
        config = {
            "type": "github",
            "url": "https://github.com",
            "password": "token",
            "canonical_url": "https://canonical.example.com",
        }
        manager = GithubManager("test", config)

        url = "https://github.com/user/repo.git"
        result = manager.canonize(url)

        assert result == "https://canonical.example.com/user/repo.git"

    @patch("unfurl.cloudmap.github.Github")
    @patch("unfurl.cloudmap.github.Auth")
    def test_canonize_without_canonical_url(self, mock_auth, mock_github_class):
        """Test URL canonization without canonical URL."""
        config = {"type": "github", "url": "https://github.com", "password": "token"}
        manager = GithubManager("test", config)

        url = "https://github.com/user/repo.git"
        result = manager.canonize(url)

        assert result == url

    @patch("unfurl.cloudmap.github.Github")
    @patch("unfurl.cloudmap.github.Auth")
    def test_url_credentials_priority(self, mock_auth, mock_github_class):
        """Test that URL credentials take priority over config credentials."""
        config = {
            "type": "github",
            "url": "https://urluser:urltoken@github.com",
            "user": "configuser",
            "password": "configtoken",
        }
        manager = GithubManager("test", config)

        assert manager.user == "urluser"
        assert manager.token == "urltoken"

    @patch("unfurl.cloudmap.github.Github")
    @patch("unfurl.cloudmap.github.Auth")
    def test_config_credentials_fallback(self, mock_auth, mock_github_class):
        """Test that config credentials are used when URL has none."""
        config = {
            "type": "github",
            "url": "https://github.com",
            "user": "configuser",
            "password": "configtoken",
        }
        manager = GithubManager("test", config)

        assert manager.user == "configuser"
        assert manager.token == "configtoken"

    @patch("unfurl.cloudmap.github.Github")
    @patch("unfurl.cloudmap.github.Auth")
    def test_url_user_only_priority(self, mock_auth, mock_github_class):
        """Test that URL username takes priority even without URL password."""
        config = {
            "type": "github",
            "url": "https://urluser@github.com",
            "user": "configuser",
            "password": "configtoken",
        }
        manager = GithubManager("test", config)

        assert manager.user == "urluser"
        assert manager.token == "configtoken"  # Falls back to config password


class TestGitlabManager:
    """Unit tests for GitlabManager using mock GitLab API objects."""

    @patch("unfurl.cloudmap.gitlab.gitlab.Gitlab")
    def test_url_credentials_priority(self, mock_gitlab_class):
        """Test that URL credentials take priority over config credentials."""
        config = {
            "type": "gitlab",
            "url": "https://urluser:urltoken@gitlab.example.com/namespace",
            "user": "configuser",
            "password": "configtoken",
        }

        # Mock the Gitlab instance to avoid auth attempt
        mock_gitlab_instance = Mock()
        mock_gitlab_class.return_value = mock_gitlab_instance

        manager = GitlabManager("test", config)

        assert manager.user == "urluser"
        assert manager.token == "urltoken"

    @patch("unfurl.cloudmap.gitlab.gitlab.Gitlab")
    def test_config_credentials_fallback(self, mock_gitlab_class):
        """Test that config credentials are used when URL has none."""
        config = {
            "type": "gitlab",
            "url": "https://gitlab.example.com/namespace",
            "user": "configuser",
            "password": "configtoken",
        }

        # Mock the Gitlab instance to avoid auth attempt
        mock_gitlab_instance = Mock()
        mock_gitlab_class.return_value = mock_gitlab_instance

        manager = GitlabManager("test", config)

        assert manager.user == "configuser"
        assert manager.token == "configtoken"

    @patch("unfurl.cloudmap.gitlab.gitlab.Gitlab")
    def test_url_user_only_priority(self, mock_gitlab_class):
        """Test that URL username takes priority even without URL password."""
        config = {
            "type": "gitlab",
            "url": "https://urluser@gitlab.example.com/namespace",
            "user": "configuser",
            "password": "configtoken",
        }

        # Mock the Gitlab instance to avoid auth attempt
        mock_gitlab_instance = Mock()
        mock_gitlab_class.return_value = mock_gitlab_instance

        manager = GitlabManager("test", config)

        assert manager.user == "urluser"
        assert manager.token == "configtoken"  # Falls back to config password

    @patch("unfurl.cloudmap.gitlab.gitlab.Gitlab")
    def test_no_credentials(self, mock_gitlab_class):
        """Test manager initialization with no credentials at all."""
        config = {
            "type": "gitlab",
            "url": "https://gitlab.example.com/namespace",
        }

        # Mock the Gitlab instance to avoid auth attempt
        mock_gitlab_instance = Mock()
        mock_gitlab_class.return_value = mock_gitlab_instance

        manager = GitlabManager("test", config)

        assert manager.user is None
        assert manager.token is None


def test_join_resource_url_merges_query_params_with_join_precedence():
    merged = join_resource_url(
        "pkg:oci/odoo?repository_url=docker.io/bitnami/odoo&tag=latest&source=base",
        "?tag=1.0&new=value",
    )
    assert (
        merged
        == "pkg:oci/odoo?repository_url=docker.io/bitnami/odoo&source=base&tag=1.0&new=value"
    )


def test_join_resource_url_replaces_repeated_base_key_with_join_values():
    merged = join_resource_url(
        "pkg:oci/name?tag=base1&tag=base2&keep=yes", "?tag=a&tag=b"
    )
    assert merged == "pkg:oci/name?keep=yes&tag=a&tag=b"


def test_join_resource_url_uri_template_version_keys():
    """A version key that is a URI template sets the url part its operator names."""
    # "?" starts a query string, "&" continues one -- either way the parameters
    # the expression sets replace the base's, like literal query keys do
    purl = "pkg:oci/odoo?repository_url=docker.io/bitnami/odoo&tag=latest"
    merged = "pkg:oci/odoo?repository_url=docker.io/bitnami/odoo{&tag}"
    assert join_resource_url(purl, "{?tag}") == merged
    assert join_resource_url(purl, "{&tag}") == merged
    assert (
        join_resource_url("pkg:oci/name?tag=base1&tag=base2&keep=yes", "{?tag,digest}")
        == "pkg:oci/name?keep=yes{&tag,digest}"
    )
    assert (
        join_resource_url("https://example.com/app", "{?tag}")
        == "https://example.com/app{?tag}"
    )
    assert (
        join_resource_url("https://example.com/app?a=1#frag", "{?tag}")
        == "https://example.com/app?a=1{&tag}#frag"
    )
    # a url has one query and one fragment, so an expression setting the same
    # part of the base is replaced, not appended to
    assert (
        join_resource_url("https://example.com/app{?tag,digest}", "{?tag}")
        == "https://example.com/app{?tag}"
    )
    assert (
        join_resource_url("https://example.com/app?a=1{&tag}", "{?tag}")
        == "https://example.com/app?a=1{&tag}"
    )
    assert (
        join_resource_url("https://example.com/app{#version}", "{#v2}")
        == "https://example.com/app{#v2}"
    )
    # "#" is the fragment, "/", ";" and "." extend the path
    assert (
        join_resource_url("https://example.com/app#v1", "{#version}")
        == "https://example.com/app{#version}"
    )
    assert join_resource_url("git://x.com/r.git", "{#branch}") == (
        "git://x.com/r.git{#branch}"
    )
    assert (
        join_resource_url("https://example.com/app?a=1", "{/segment}")
        == "https://example.com/app{/segment}?a=1"
    )
    assert (
        join_resource_url("https://example.com/app", "{;matrix}")
        == "https://example.com/app{;matrix}"
    )
    assert (
        join_resource_url("https://example.com/app", "{.label}")
        == "https://example.com/app{.label}"
    )
    # "+" can expand into a whole url, so the key is used as-is
    assert join_resource_url("https://example.com/app", "{+urlvar}") == "{+urlvar}"
    assert join_resource_url("git://a.com/x.git", "{+urlvar}") == "{+urlvar}"
    # the default operator expands to a single percent-encoded value, so the key
    # is merged like a bare version key: a git ref for a git url, else as-is
    assert join_resource_url("https://example.com/app", "{version}") == "{version}"
    assert join_resource_url("git://a.com/x.git", "{version}") == (
        "git://a.com/x.git#{version}"
    )
    assert join_resource_url("git://a.com/x.git#:f.yaml", "{version}") == (
        "git://a.com/x.git#{version}:f.yaml"
    )
    # the operators RFC 6570 reserves aren't valid, so those keys are plain too
    assert (
        join_resource_url("git://a.com/x.git", "{=var}") == "git://a.com/x.git#{=var}"
    )
    assert join_resource_url("https://example.com/app", "{,var}") == "{,var}"


def test_uri_template_artifact_urls():
    """Artifact keys can be URI templates; an expression isn't a label."""
    assert Artifact(url="git://a.com/x.git{#ref}").get_repository_url() == (
        "git://a.com/x.git{#ref}"
    )
    # an expression can expand into the ".git" suffix, so it isn't added
    assert Artifact(url="git://a.com/x{#ref}#v1.0:.").get_repository_url() == (
        "git://a.com/x{#ref}"
    )
    assert Artifact(url="git://a.com/x#v1.0:.").get_repository_url() == (
        "git://a.com/x.git"
    )
    # a template key is not a label, so it is treated as a url (rust's is_url()
    # makes the same distinction)
    assert not is_label("{+urlvar}")
    assert not is_label("docker.io/{name}")
    assert not is_label("{version}")
    assert is_label("v1.0")


def test_uri_template_record_keys():
    """URI templates are usable as record keys, including as version keys."""
    doc = {
        "apiVersion": API_VERSION,
        "kind": "CloudMap",
        "artifacts": {
            "pkg:oci/odoo?repository_url=docker.io/bitnami/odoo&tag=latest": {
                "versions": {"{?tag}": {}}
            },
            # a template can expand into the whole url, including the scheme
            "{+urlvar}": {},
        },
        "services": {
            "https://example.com/app": {"versions": {"{#version}": {}}},
        },
    }
    db = CloudMapDB("cloudmap.yaml", contents=doc, validate=False)
    assert set(db.artifacts) == {
        "pkg:oci/odoo?repository_url=docker.io/bitnami/odoo&tag=latest",
        "pkg:oci/odoo?repository_url=docker.io/bitnami/odoo{&tag}",
        "{+urlvar}",
    }
    assert set(db.services) == {
        "https://example.com/app",
        "https://example.com/app{#version}",
    }
    # and they can be looked up by pseudo-URL
    assert db.get_artifact("artifact:{+urlvar}") is db.artifacts["{+urlvar}"]
    version_url = "https://example.com/app{#version}"
    assert db.get_service(f"service:[{version_url}]") is db.services[version_url]


def test_cloudmap_schema_typed_url_keys():
    """A typedURLs key is a url, a URI template or a type name -- never two of them.

    The nested ``{label: {url: typeRef}}`` form is a ``oneOf`` between a map of
    type names and a map of urls, so a key matching both patterns invalidates
    the whole document.
    """
    from unfurl.util import UnfurlSchemaError

    def validates(references) -> bool:
        doc = {
            "apiVersion": API_VERSION,
            "kind": "CloudMap",
            "artifacts": {"pkg:oci/x": {"references": references}},
        }
        try:
            CloudMapDB("cloudmap.yaml", contents=doc, validate=True)
            return True
        except UnfurlSchemaError:
            return False

    for key in (
        "git://x/r.git",  # a url
        "pkg:oci/n",
        "{+urlvar}",  # a URI template, which is a url, not a type name
        "{?tag}",
        "software.Nginx",  # a type name
        "WebApp@unfurl.cloud/onecommons/std:generic",
    ):
        assert validates({"the_db": {key: None}}), key
        assert validates({key: None}), key
    # a label is only a key of the outer map, and whitespace is never a key
    assert validates({"the_db": None})
    assert not validates({"has space": None})
    assert not validates({"the_db": {"has space": None}})


def test_cloudmap_schema_with_artifacts_and_services():
    """Test CloudMapDB validates artifacts and services sections with new schema."""
    import tempfile

    cloudmap_yaml = f"""apiVersion: {API_VERSION}
kind: CloudMap
repositories:
  github.com/onecommons/unfurl:
    git: github.com/onecommons/unfurl.git
    path: onecommons/unfurl
    name: unfurl
    protocols:
    - https
    - ssh
    default_branch: main
    branches:
      main: f5da8de13ae2dcce293508c4ccac9b373e66dd49
    tags:
      v1.1.0: abc123def456
instantiations:
  "2023-09-24T15:30:00Z":
    type:
      cloudmap.artifacts.IntotoAttestation: null
    source: https://github.com/onecommons/unfurl#:.
    source_revision: f5da8de13ae2dcce293508c4ccac9b373e66dd49
  "2023-09-24T15:31:00Z":
    type:
      cloudmap.artifacts.unfurl.Ensemble: null
    source: git://unfurl.cloud/onecommons/unfurl_cloud_prod.git#:v1:prod
    revision: f5da8de13ae2dcce293508c4ccac9b373e66dd49
artifacts:
  pkg:oci:docker.io/library/nginx:
    type:
      cloudmap.artifacts.oci.Image:
    contains:
      pkg:oci:ghcr.io/library/alpine:
    instantiates:
      web:
        software.WebServer:
          version: "1.25"
      http:
        software.HTTPServer:
    dependencies:
      "os":
        software.Linux:
          version: ">=5.0"
    instantiated_by:
      "#/instantiations/2023-09-24T15:30:00Z":
    digest: sha256:abc123
    immutable: false
    metadata:
      title: Nginx Web Server
      description: High-performance HTTP server and reverse proxy
      created: "2023-09-24T15:30:00Z"
      platforms:
      - architecture: amd64
        os: linux
      - architecture: arm64
        os: linux
      spdx_licenses: BSD-2-Clause
      vendor: Nginx Inc.
      version: 1.25.3
      homepage_url: https://nginx.org
      documentation_url: https://nginx.org/en/docs/
      thumbnail_url: https://nginx.org/images/nginx-logo.png
      discovery:
        last_checked: "2023-09-24T15:30:00Z"
        sources:
        - https://ghcr.io/v2/nginx/manifests/latest
        - https://github.com/nginx/nginx/releases
    versions:
      "@sha256:f5da8de13ae2dcce293508c4ccac9b373e66dd49":
        digest: sha256:f5da8de13ae2dcce293508c4ccac9b373e66dd49
        immutable: true
      ?tag=latest:
        digest: sha256:f5da8de13ae2dcce293508c4ccac9b373e66dd49
        immutable: false
        metadata:
          version: latest
services:
  https://unfurl.cloud:
    type:
      WebApp@unfurl.cloud/onecommons/std:generic_types:
        version: "1.0"
      capabilities.GitOps:
      capabilities.CICD:
        version: ">=2.0"
    endpoints:
      https://unfurl.cloud/api/v4:
        GitLabAPI:
          version: "4"
    connections:
      https://github.com:
    metadata:
      title: Unfurl Cloud
      description: Open-source platform for collaboratively developing cloud applications
      vendor: OneCommons
      version: 1.0.0
      documentation_url: https://docs.unfurl.cloud
      thumbnail_url: https://unfurl.cloud/unfurl-logo.svg
      source_url: https://github.com/onecommons/unfurl-cloud
      spdx_licenses: MIT
      discovery:
        last_checked: "2023-09-24T15:30:00Z"
        sources:
        - https://unfurl.cloud/api/v1/metadata
    policies:
      terms_of_service: https://unfurl.cloud/terms
      privacy_policy: https://unfurl.cloud/privacy
    instantiated_by:
      "#/instantiations/2023-09-24T15:31:00Z":
types:
  Zulip@unfurl.cloud/onecommons/blueprints/zulip:
    kind: component
    source: git://unfurl.cloud/onecommons/blueprints/zulip.git#:types/app.yaml
    metadata:
      title: Zulip
    extends:
    - unfurl.nodes.WebApp@unfurl.cloud/onecommons/std:generic_types
    - WebApp@unfurl.cloud/onecommons/std:generic_types
  software.Nginx@unfurl.cloud/onecommons/std:
    kind: component
    metadata:
      title: Nginx Web Server
    extends:
    - software.WebServer@unfurl.cloud/onecommons/std:generic_types
"""

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(cloudmap_yaml)
        temp_path = f.name

    try:
        # This should validate the schema successfully
        db = CloudMapDB(temp_path)

        # Verify repositories loaded correctly
        assert len(db.repositories) == 1
        assert "git://github.com/onecommons/unfurl.git" in db.repositories

        # Verify artifacts loaded correctly
        assert "artifacts" in db.db
        assert "pkg:oci:docker.io/library/nginx" in db.artifacts
        artifact = db.get_artifact("pkg:oci:docker.io/library/nginx")
        assert artifact
        assert isinstance(artifact.type, TypeRefs)
        assert "cloudmap.artifacts.oci.Image" in artifact.type.types
        assert artifact.immutable is False
        assert len(artifact.contains) == 1
        assert len(artifact.instantiated_by) == 1

        # Verify instantiations loaded correctly
        assert "instantiations" in db.db
        assert len(db.instantiations) == 2

        # Get the build instantiation (ignore exact timestamp key)
        build_instantiation = None
        for key, inst in db.instantiations.items():
            if "cloudmap.artifacts.IntotoAttestation" in inst.type.types:
                build_instantiation = inst
                assert inst.source == "https://github.com/onecommons/unfurl#:."
                assert (
                    inst.source_revision == "f5da8de13ae2dcce293508c4ccac9b373e66dd49"
                )
                break
        assert build_instantiation is not None, "Build instantiation not found"

        # Verify instantiates uses typedURLs structure
        instantiates = artifact.instantiates
        assert isinstance(instantiates, dict)
        # label-form entries are keyed by (label, "")
        assert ("web", "") in instantiates
        web = instantiates[("web", "")]
        assert isinstance(web, TypeRefs)
        assert web.types["software.WebServer"]["version"] == "1.25"
        assert ("http", "") in instantiates
        http = instantiates[("http", "")]
        assert isinstance(http, TypeRefs)
        assert http.types["software.HTTPServer"] is None

        # Verify dependencies uses typeRef structure
        assert artifact.dependencies == {
            ("os", ""): TypeRefs({"software.Linux": {"version": ">=5.0"}})
        }

        assert artifact.metadata.title == "Nginx Web Server"
        assert len(artifact.metadata.platforms) == 2
        assert artifact.metadata.discovery.last_checked == "2023-09-24T15:30:00Z"
        assert len(artifact.metadata.discovery.sources) == 2

        # Verify versions loaded correctly
        versions = artifact.versions
        assert len(versions) == 2
        version_by_digest = db.get_artifact(
            "pkg:oci:docker.io/library/nginx@sha256:f5da8de13ae2dcce293508c4ccac9b373e66dd49"
        )
        assert version_by_digest, list(db.artifacts)
        assert "@sha256:f5da8de13ae2dcce293508c4ccac9b373e66dd49" in versions
        # assert "pkg:oci:docker.io/library/nginx:latest" in versions

        # Verify version by digest
        assert (
            version_by_digest.digest
            == "sha256:f5da8de13ae2dcce293508c4ccac9b373e66dd49"
        )
        assert version_by_digest.immutable is True

        # Verify version by tag
        version_latest = db.get_artifact("pkg:oci:docker.io/library/nginx?tag=latest")
        assert version_latest, list(db.artifacts)
        assert (
            version_latest.digest == "sha256:f5da8de13ae2dcce293508c4ccac9b373e66dd49"
        )
        assert version_latest.immutable is False
        assert version_latest.metadata.version == "latest"

        # Verify services loaded correctly
        assert "services" in db.db
        assert "https://unfurl.cloud" in db.services
        service = db.services["https://unfurl.cloud"]

        # Verify type uses typeRef structure
        service_type = service.type
        assert isinstance(service_type, TypeRefs)
        assert "WebApp@unfurl.cloud/onecommons/std:generic_types" in service_type.types
        assert (
            service_type.types["WebApp@unfurl.cloud/onecommons/std:generic_types"][
                "version"
            ]
            == "1.0"
        )

        # Verify capabilities uses typeRef structure (capabilities are in service.type)
        assert "capabilities.GitOps" in service_type.types
        assert service_type.types["capabilities.GitOps"] is None
        assert "capabilities.CICD" in service_type.types
        assert service_type.types["capabilities.CICD"]["version"] == ">=2.0"

        assert len(service.endpoints) == 1
        assert len(service.connections) == 1
        assert service.metadata.title == "Unfurl Cloud"
        assert service.metadata.spdx_licenses == "MIT"
        assert len(service.instantiated_by) == 1
        assert service.metadata.discovery.last_checked == "2023-09-24T15:30:00Z"
        assert len(service.metadata.discovery.sources) == 1

        # Get the deployment instantiation (ignore exact timestamp key)
        deployment_instantiation = None
        for key, inst in db.instantiations.items():
            if "cloudmap.artifacts.unfurl.Ensemble" in inst.type.types:
                deployment_instantiation = inst
                assert (
                    inst.source
                    == "git://unfurl.cloud/onecommons/unfurl_cloud_prod.git#:v1:prod"
                )
                assert inst.revision == "f5da8de13ae2dcce293508c4ccac9b373e66dd49"
                break
        assert deployment_instantiation is not None, (
            "Deployment instantiation not found"
        )

        # Verify types loaded correctly
        assert "types" in db.db
        assert len(db.types) == 2

        # Verify Zulip service type
        assert "Zulip@unfurl.cloud/onecommons/blueprints/zulip" in db.types
        zulip_type = db.types["Zulip@unfurl.cloud/onecommons/blueprints/zulip"]
        assert zulip_type.kind == "component"
        assert zulip_type.metadata.title == "Zulip"
        assert (
            zulip_type.source
            == "git://unfurl.cloud/onecommons/blueprints/zulip.git#:types/app.yaml"
        )

        # Verify extends is an array of strings
        extends = zulip_type.extends
        assert isinstance(extends, list)
        assert len(extends) == 2
        assert (
            "unfurl.nodes.WebApp@unfurl.cloud/onecommons/std:generic_types" in extends
        )
        assert "WebApp@unfurl.cloud/onecommons/std:generic_types" in extends

        # Verify Nginx software type
        assert "software.Nginx@unfurl.cloud/onecommons/std" in db.types
        nginx_type = db.types["software.Nginx@unfurl.cloud/onecommons/std"]
        assert nginx_type.kind == "component"
        assert nginx_type.metadata.title == "Nginx Web Server"
        assert not nginx_type.source  # Optional field not provided

        # Verify extends for Nginx is an array
        nginx_extends = nginx_type.extends
        assert isinstance(nginx_extends, list)
        assert len(nginx_extends) == 1
        assert (
            "software.WebServer@unfurl.cloud/onecommons/std:generic_types"
            in nginx_extends
        )

    finally:
        # Clean up temp file
        os.unlink(temp_path)


def test_get_cloudmap_types(mocker):
    """Test get_cloudmap_types with mocked load_yaml_from_cache."""
    from unfurl.server.cache import get_cloudmap_types, CLOUDMAP_BRANCH
    import yaml

    # Parse the expected_types_cloudmap YAML string
    cloudmap_doc = yaml.safe_load(expected_types_cloudmap)

    # Variable to capture the CloudMapDB instance
    captured_db = None

    # Create a wrapper for CloudMapDB to capture the instance
    original_cloudmapdb = CloudMapDB

    def cloudmapdb_wrapper(*args, **kwargs):
        nonlocal captured_db
        captured_db = original_cloudmapdb(*args, **kwargs)
        return captured_db

    # Mock load_yaml_from_cache to return the parsed cloudmap
    with patch("unfurl.server.cache.load_yaml_from_cache") as mock_load_yaml:
        mock_load_yaml.return_value = (None, cloudmap_doc)

        # Spy on CloudMapDB to capture the instance
        with patch("unfurl.server.cache.CloudMapDB", side_effect=cloudmapdb_wrapper):
            # Create a mock CacheEntry (we just need something to pass in)
            mock_cache_entry = Mock()

            # Call the function
            err, types = get_cloudmap_types("test_project", mock_cache_entry, True)

            # Verify load_yaml_from_cache was called correctly
            mock_load_yaml.assert_called_once_with(
                "test_project",
                CLOUDMAP_BRANCH,
                "cloudmap.yaml",
                mock_cache_entry,
                None,
            )

            # Verify no error
            assert err is None

            # Verify the db was captured
            assert isinstance(captured_db, CloudMapDB)
            assert captured_db.get_repository(
                "git://unfurl.cloud/onecommons/blueprints/cronicle.git#:ensemble-template.yaml%23spec/service_template"
            )

            # Verify we got the expected type
            assert "CronicleApp@unfurl.cloud/onecommons/blueprints/cronicle" in types

            # Verify the type has the expected properties
            cronicle_type = types[
                "CronicleApp@unfurl.cloud/onecommons/blueprints/cronicle"
            ]
            assert cronicle_type == {
                "__typename": "ResourceType",
                "name": "CronicleApp@unfurl.cloud/onecommons/blueprints/cronicle",
                "requirements": [],
                "extends": [
                    "CronicleApp@unfurl.cloud/onecommons/blueprints/cronicle",
                    "unfurl.nodes.WebApp@unfurl.cloud/onecommons/std:generic_types",
                    "WebApp@unfurl.cloud/onecommons/std:generic_types",
                    "_ContainerAppBase@unfurl.cloud/onecommons/std:generic_types",
                    "App@unfurl.cloud/onecommons/std:generic_types",
                    "tosca.nodes.Root",
                    "tosca.capabilities.Node",
                    "tosca.capabilities.Root",
                ],
                "title": "CronicleApp",
                "_sourceinfo": {
                    "file": "ensemble-template.yaml#spec/service_template",
                    "url": "https://unfurl.cloud/onecommons/blueprints/cronicle.git",
                    "incomplete": True,
                },
                "inputsSchema": {},
                "description": "A simple, distributed task scheduler and runner with a web based UI.",
                "implementations": ["connect", "create"],
                "directives": ["substitute"],
                "icon": "https://unfurl.cloud/onecommons/blueprints/cronicle/-/avatar",
            }


# ---------------------------------------------------------------------------
# /cloudmap endpoint tests
#
# Mirror the Rust integration tests in
# rust/server/tests/test_cloudmap.rs. They follow the
# test_get_cloudmap_types() pattern: mock load_yaml_from_cache so the handler
# operates on a fixed cloudmap dict, then exercise the route via
# Flask's test client.
#
# We load the same fixture used by the Rust crate so the BFS expectations
# stay in lock-step across both implementations.
# ---------------------------------------------------------------------------


def _load_cloudmap_fixture():
    """Read tests/fixtures/expected_cloudmap.yaml."""
    import yaml as _yaml

    here = os.path.dirname(os.path.abspath(__file__))
    fixture_path = os.path.join(here, "fixtures", "expected_cloudmap.yaml")
    with open(fixture_path) as f:
        return _yaml.safe_load(f)


@pytest.fixture
def cloudmap_test_client():
    """Yield a Flask test client wired to the unfurl server app, with
    `unfurl.server.cache.load_yaml_from_cache` patched to return the cloudmap
    fixture from ``tests/fixtures/``."""
    from unfurl.server.serve import app

    cloudmap_doc = _load_cloudmap_fixture()
    with patch("unfurl.server.cache.load_yaml_from_cache") as mock_load_yaml:
        mock_load_yaml.return_value = (None, cloudmap_doc)
        with app.test_client() as client:
            yield client


def test_cloudmap_endpoint_full_document(cloudmap_test_client):
    resp = cloudmap_test_client.get("/cloudmap")
    assert resp.status_code == 200
    body = resp.get_json()
    assert isinstance(body, dict)
    primary = body["result"]
    assert "repositories" in primary
    assert "artifacts" in primary
    # no key was supplied, so nothing was followed and the key is absent
    assert "followed" not in body


def test_cloudmap_endpoint_kind_only(cloudmap_test_client):
    resp = cloudmap_test_client.get("/cloudmap?kind=repositories")
    assert resp.status_code == 200
    body = resp.get_json()
    primary = body["result"]
    assert "repositories" in primary
    assert "artifacts" not in primary
    assert "followed" not in body


def test_cloudmap_endpoint_kind_and_key(cloudmap_test_client):
    from urllib.parse import quote

    key = "git://unfurl.cloud/onecommons/blueprints/odoo.git"
    resp = cloudmap_test_client.get(f"/cloudmap?kind=repositories&key={quote(key)}")
    assert resp.status_code == 200
    body = resp.get_json()
    primary = body["result"]
    assert list(primary["repositories"].keys()) == [key]
    assert "followed" not in body


def test_cloudmap_endpoint_missing_key(cloudmap_test_client):
    resp = cloudmap_test_client.get(
        "/cloudmap?kind=repositories&key=git://no/such/repo.git"
    )
    assert resp.status_code == 404


def test_cloudmap_endpoint_follow_walks_graph(cloudmap_test_client):
    from urllib.parse import quote

    key = "git://unfurl.cloud/onecommons/blueprints/odoo.git"
    resp = cloudmap_test_client.get(
        f"/cloudmap?kind=repositories&key={quote(key)}&follow=10"
    )
    assert resp.status_code == 200
    body = resp.get_json()
    followed = body["followed"]
    assert followed, "follow=10 should reach at least one record"

    # Collect every key across every section in the followed dict.
    keys = []
    for section, records in followed.items():
        for k in records:
            keys.append(k)
    keys.sort()
    # The Python CloudMapGraphWalker traverses more edges than the Rust
    # BFS — it follows type references and the original (un-normalised)
    # artifact URLs — so the reachable set is larger and slightly
    # different from rust/server/tests/test_cloudmap.rs.
    expected = sorted([
        "Odoo@unfurl.cloud/onecommons/blueprints/odoo",
        "PostgresDB@unfurl.cloud/onecommons/unfurl-types",
        "git://unfurl.cloud/onecommons/blueprints/odoo.git#:.gitlab-ci.yml",
        "git://unfurl.cloud/onecommons/blueprints/odoo.git#:ensemble-template.yaml%23spec/service_template",
        "git://unfurl.cloud/onecommons/unfurl-types#v0.7.7:.",
        "git://unfurl.cloud/onecommons/unfurl-types.git#:.gitlab-ci.yml",
        "git://unfurl.cloud/onecommons/unfurl-types.git#:dummy-ensemble.yaml",
        "pkg:oci/odoo?repository_url=docker.io/bitnami/odoo&tag=latest",
        "unfurl.relationships.ConnectsTo.AWSAccount",
        "unfurl.relationships.ConnectsTo.GoogleCloudProject",
    ])
    assert keys == expected

    ensemble = followed["artifacts"][
        "git://unfurl.cloud/onecommons/blueprints/odoo.git#:ensemble-template.yaml%23spec/service_template"
    ]
    assert "type" in ensemble


def test_cloudmap_endpoint_follow_caps_record_count(cloudmap_test_client):
    from urllib.parse import quote

    key = "git://unfurl.cloud/onecommons/blueprints/odoo.git"
    resp = cloudmap_test_client.get(
        f"/cloudmap?kind=repositories&key={quote(key)}&follow=2"
    )
    assert resp.status_code == 200
    body = resp.get_json()
    total = sum(len(records) for records in body["followed"].values())
    assert total == 2


def test_cloudmap_endpoint_follow_zero(cloudmap_test_client):
    from urllib.parse import quote

    key = "git://unfurl.cloud/onecommons/blueprints/odoo.git"
    resp = cloudmap_test_client.get(
        f"/cloudmap?kind=repositories&key={quote(key)}&follow=0"
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert "followed" not in body


def test_cloudmap_endpoint_follow_without_key_walks_from_the_matches(
    cloudmap_test_client,
):
    """A walk needs no key: it starts from every record the query selected,
    so a filtered query returns its neighbourhood."""
    resp = cloudmap_test_client.get("/cloudmap?kind=repositories&follow=100")
    assert resp.status_code == 200
    body = resp.get_json()
    roots = set(body["result"]["repositories"])
    assert roots, "fixture should have repositories to walk from"

    followed = body["followed"]
    assert followed, "the repositories reference records in other sections"
    # The roots are already in `result`; `followed` is what they reach.
    assert "repositories" not in followed or not (set(followed["repositories"]) & roots)
    assert set(followed) - {"repositories"}, (
        f"should reach other sections: {list(followed)}"
    )


def test_cloudmap_endpoint_follow_caps_a_keyless_walk(cloudmap_test_client):
    resp = cloudmap_test_client.get("/cloudmap?kind=repositories&follow=3")
    assert resp.status_code == 200
    followed = resp.get_json()["followed"]
    total = sum(len(records) for records in followed.values())
    assert total == 3


def test_cloudmap_endpoint_follow_reports_edge_derived_keys(cloudmap_test_client):
    """Even with every record as a root, a walk still discovers entries:
    an edge names a record by a url the section doesn't key it under --
    a derived artifact url, a type-qualified repository name -- so what
    comes back under ``followed`` is keyed the way the edge referred to it."""
    resp = cloudmap_test_client.get("/cloudmap?follow=10")
    assert resp.status_code == 200
    body = resp.get_json()
    followed = body["followed"]
    assert followed, "edges name records by urls that are not section keys"
    total = sum(len(records) for records in followed.values())
    assert total <= 10, "follow caps the walk"
    # None of them repeats a key already in the result.
    for section_name, records in followed.items():
        roots = set(body["result"].get(section_name, {}))
        assert not (set(records) & roots), section_name


# ---------------------------------------------------------------------------
# GET /cloudmap paging
# ---------------------------------------------------------------------------

from urllib.parse import quote


def _walk_pages(client, url, page_size):
    """Page through ``url`` and return the accumulated (section, key) pairs
    in the order the server returned them, plus the number of requests."""
    seen = []
    token = None
    requests = 0
    while True:
        paged = f"{url}&limit={page_size}"
        if token:
            paged += f"&page_token={quote(token)}"
        resp = client.get(paged)
        requests += 1
        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = resp.get_json()
        assert set(body) <= {"result", "next_page_token"}, (
            "a paged request is keyless, so it never asks to follow"
        )
        for section, entries in body["result"].items():
            for k in entries:
                seen.append((section, k))
        token = body.get("next_page_token")
        if not token:
            return seen, requests
        assert requests < 100, "paging failed to terminate"


def test_cloudmap_endpoint_paged_walk_matches_unpaged(cloudmap_test_client):
    """Pages concatenate to exactly the unpaged section: no gaps, no repeats."""
    unpaged = cloudmap_test_client.get("/cloudmap?kind=repositories").get_json()[
        "result"
    ]
    expected = sorted(unpaged["repositories"])

    seen, requests = _walk_pages(cloudmap_test_client, "/cloudmap?kind=repositories", 2)
    assert requests > 1, "fixture should need several pages at limit=2"
    assert [s for s, _ in seen] == ["repositories"] * len(seen)
    keys = [k for _, k in seen]
    assert keys == sorted(keys), "records come back in key order"
    assert keys == expected


def test_cloudmap_endpoint_paged_full_document(cloudmap_test_client):
    """Paging the whole document spans sections in path order and drops
    the envelope keys, which aren't records."""
    seen, _ = _walk_pages(cloudmap_test_client, "/cloudmap?", 3)
    sections = [s for s, _ in seen]
    assert "apiVersion" not in sections and "metadata" not in sections
    # section order follows "/"-prefixed path order, and each section's
    # records are contiguous
    assert sections == sorted(sections, key=lambda s: "/" + s)
    unpaged = cloudmap_test_client.get("/cloudmap").get_json()["result"]
    expected = {
        (section, k)
        for section, entries in unpaged.items()
        if isinstance(entries, dict) and section not in ("metadata",)
        for k in entries
    }
    assert set(seen) == expected


def test_cloudmap_endpoint_paging_last_page_has_no_token(cloudmap_test_client):
    """A limit at or above the section size answers in one page."""
    unpaged = cloudmap_test_client.get("/cloudmap?kind=repositories").get_json()[
        "result"
    ]
    size = len(unpaged["repositories"])
    for limit in (size, size + 5):
        body = cloudmap_test_client.get(
            f"/cloudmap?kind=repositories&limit={limit}"
        ).get_json()
        assert "next_page_token" not in body, f"limit={limit} should be one page"
        assert len(body["result"]["repositories"]) == size


def test_cloudmap_endpoint_paging_empty_result(cloudmap_test_client):
    """A page with nothing in it is an empty document, not an error."""
    resp = cloudmap_test_client.get(
        "/cloudmap?kind=artifacts&type=no.such.Type&limit=5"
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["result"] == {}
    assert "next_page_token" not in body


def test_cloudmap_endpoint_paging_with_type_filter(cloudmap_test_client):
    """`type` narrows before the page is cut, so a paged walk and an
    unpaged query return the same records."""
    url = "/cloudmap?kind=artifacts&type=cloudmap.artifacts.GitLabPipeline"
    unpaged = cloudmap_test_client.get(url).get_json()["result"]
    expected = sorted(unpaged.get("artifacts", {}))
    assert expected, "fixture should have GitLabPipeline artifacts"

    seen, _ = _walk_pages(cloudmap_test_client, url, 1)
    assert sorted(k for _, k in seen) == expected


def test_cloudmap_endpoint_paging_rejects_key(cloudmap_test_client):
    key = "git://unfurl.cloud/onecommons/blueprints/odoo.git"
    resp = cloudmap_test_client.get(
        f"/cloudmap?kind=repositories&key={quote(key)}&limit=2"
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["code"] == "BAD_REQUEST", body
    assert "limit cannot be combined with key" in body["message"]


def test_graph_endpoint_reports_a_missing_record(cloudmap_test_client):
    """/graph answers a URL it can't find with a `NOT_FOUND` ErrorResponse.

    `cloudmap_graph_json` reports a missing record by returning a document whose
    only key is `error`, which used to be served as the response body as-is --
    a shape found nowhere else and in no schema.
    """
    resp = cloudmap_test_client.get("/graph?url=nonexistent://url")
    assert resp.status_code == 404, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["code"] == "NOT_FOUND", body
    assert "Record not found" in body["message"], body
    assert "error" not in body, body


def test_cloudmap_endpoint_paging_follows_from_the_page(cloudmap_test_client):
    """A paged walk starts from that page's records -- never from one the
    page doesn't contain -- and the cap applies per page."""
    resp = cloudmap_test_client.get("/cloudmap?kind=repositories&limit=2&follow=10")
    assert resp.status_code == 200
    body = resp.get_json()
    page = set(body["result"]["repositories"])
    assert len(page) == 2
    followed = body["followed"]
    assert followed, "the page's repositories reference other records"
    assert sum(len(v) for v in followed.values()) <= 10

    # The neighbourhood belongs to this page: walking the same two records
    # unpaged gives the same followed set.
    unpaged = cloudmap_test_client.get("/cloudmap?kind=repositories&follow=10")
    assert unpaged.status_code == 200
    # (different roots, so only assert the page's own walk is self-consistent)
    again = cloudmap_test_client.get(
        "/cloudmap?kind=repositories&limit=2&follow=10"
    ).get_json()
    assert again["followed"] == followed
    assert set(again["result"]["repositories"]) == page


def test_cloudmap_endpoint_paging_rejects_bad_limit(cloudmap_test_client):
    for bad in ("0", "-1"):
        resp = cloudmap_test_client.get(f"/cloudmap?kind=repositories&limit={bad}")
        assert resp.status_code == 422, f"limit={bad} should fail schema validation"


def test_cloudmap_endpoint_paging_rejects_bad_token(cloudmap_test_client):
    # no delimiter, empty key, and a section that isn't one -- the last
    # would otherwise resume from the wrong place in the ordering
    for bad in ("repositories", quote("repositories/"), "nosuchsection/key"):
        resp = cloudmap_test_client.get(
            f"/cloudmap?kind=repositories&limit=2&page_token={bad}"
        )
        assert resp.status_code == 400, f"page_token={bad} should be rejected"


def test_cloudmap_endpoint_paging_survives_deleted_anchor(cloudmap_test_client):
    """The cursor is a value, not a row reference: dropping the record it
    names still resumes at the right place."""
    from unfurl.server.cloudmap import _encode_page_token

    unpaged = cloudmap_test_client.get("/cloudmap?kind=repositories").get_json()[
        "result"
    ]
    keys = sorted(unpaged["repositories"])
    assert len(keys) > 2
    # a token naming a key that was never in the document at all
    token = _encode_page_token("/repositories", keys[0] + "\x01gone")
    resp = cloudmap_test_client.get(
        f"/cloudmap?kind=repositories&limit=50&page_token={quote(token)}"
    )
    assert resp.status_code == 200
    assert sorted(resp.get_json()["result"]["repositories"]) == keys[1:]


def test_page_token_round_trips_and_is_stable():
    """The wire format is pinned: the rust server mints byte-identical
    tokens, and a walk can cross between the two implementations."""
    from unfurl.server.cloudmap import _decode_page_token, _encode_page_token

    assert _encode_page_token("/artifacts", "pkg:x") == "artifacts/pkg:x"
    assert _decode_page_token(_encode_page_token("/artifacts", "pkg:x")) == (
        "/artifacts",
        "pkg:x",
    )
    # non-ASCII keys survive the round trip
    assert _decode_page_token(_encode_page_token("/types", "é-type")) == (
        "/types",
        "é-type",
    )


def test_instantiation_versions():
    """Test that Instantiation versions property works correctly with type inheritance and serialization."""
    import json

    # Create an Instantiation with versions as dicts
    inst_data = {
        "url": "test-inst",
        "type": {"software.Nginx": None},
        "versions": {
            "v1": {"digest": "sha256:abc123"},
            "v2": {
                "digest": "sha256:def456",
                "type": {"software.Nginx": {"version": "1.25"}},
            },
        },
    }

    inst = Instantiation(**inst_data)

    # Verify versions were converted to Instantiation instances
    assert isinstance(inst.versions["v1"], Instantiation)
    assert isinstance(inst.versions["v2"], Instantiation)

    # Verify type inheritance from parent
    assert inst.versions["v1"].type.types == {"software.Nginx": None}
    # v2 specifies its own type, should not inherit
    assert inst.versions["v2"].type.types == {"software.Nginx": {"version": "1.25"}}

    # Verify other properties
    assert inst.versions["v1"].digest == "sha256:abc123"
    assert inst.versions["v2"].digest == "sha256:def456"

    assert inst.versions["v2"]._parent == inst

    # Test serialization
    result = inst.asdict()
    assert "versions" in result
    assert "v1" in result["versions"]
    assert "v2" in result["versions"]
    assert result["versions"]["v1"]["digest"] == "sha256:abc123"
    assert result["versions"]["v2"]["digest"] == "sha256:def456"

    # Verify the result is JSON serializable
    json_str = json.dumps(result)
    assert json_str  # Should not raise an exception

    # Test round-trip: deserialize and recreate
    recreated = Instantiation(url="test-inst", **result)
    assert isinstance(recreated.versions["v1"], Instantiation)
    assert recreated.versions["v1"].digest == "sha256:abc123"


def test_release_schedule():
    """Test that Service release_schedule property works correctly with serialization."""
    import json

    # Create a Service with release_schedule (formerly migrations)
    service_data = {
        "url": "https://example.com/api",
        "type": {"service.API": None},
        "status": "production",
        "release_schedule": [
            {
                "url": "https://new-example.com/api",
                "status": "production",
                "effective_date": "2026-03-01T00:00:00Z",
            },
            {
                "url": "https://example.com/api/v2",
                "status": "beta",
                "effective_date": "2026-02-15T00:00:00Z",
            },
        ],
    }

    service = Service(**service_data)

    # Verify release_schedule field
    assert len(service.release_schedule) == 2
    assert service.release_schedule[0].url == "https://new-example.com/api"
    assert service.release_schedule[0].status == "production"
    assert service.release_schedule[0].effective_date == "2026-03-01T00:00:00Z"
    assert service.release_schedule[1].url == "https://example.com/api/v2"
    assert service.release_schedule[1].status == "beta"
    assert service.release_schedule[1].effective_date == "2026-02-15T00:00:00Z"

    # Test serialization
    result = service.asdict()
    assert "release_schedule" in result
    assert len(result["release_schedule"]) == 2
    assert result["release_schedule"][0]["url"] == "https://new-example.com/api"
    assert result["release_schedule"][0]["status"] == "production"
    assert result["release_schedule"][1]["url"] == "https://example.com/api/v2"

    # Verify it's JSON serializable
    json_str = json.dumps(result)
    assert json_str  # Should not raise an exception

    # Test round-trip: deserialize and recreate
    recreated = Service(url="https://example.com/api", **result)
    assert len(recreated.release_schedule) == 2
    assert recreated.release_schedule[0].url == "https://new-example.com/api"


def test_is_git_url():
    """Test the _is_git_url static method with various URL formats."""
    from unfurl.cloudmap import CloudMap

    # git scheme or .git suffix
    assert CloudMap._is_git_url("git://github.com/org/repo") is True
    assert CloudMap._is_git_url("git+https://github.com/org/repo") is True
    assert CloudMap._is_git_url("git+ssh://github.com/org/repo") is True
    assert CloudMap._is_git_url("https://github.com/org/repo.git") is True
    assert CloudMap._is_git_url("https://example.com/repo.git") is True
    assert CloudMap._is_git_url("https://example.com/repo.git#:path/file") is True
    # github.com "org/repo" paths are treated as git repos even without .git
    assert CloudMap._is_git_url("https://github.com/org/repo") is True
    assert CloudMap._is_git_url("https://github.com/org/repo/subdir") is True
    # unfurl.cloud project paths (no hyphen) are git repos too
    assert CloudMap._is_git_url("https://unfurl.cloud/org/repo") is True
    assert CloudMap._is_git_url("https://unfurl.cloud/org/repo/name") is True
    assert CloudMap._is_git_url("https://unfurl.cloud/org/repo-name") is True
    assert CloudMap._is_git_url("https://unfurl.cloud/org/repo/-/pipelines") is False
    # other hosts without a git scheme or .git suffix are not git URLs
    assert CloudMap._is_git_url("https://gitlab.com/org/repo") is False
    assert CloudMap._is_git_url("https://example.com/service") is False
    assert (
        CloudMap._is_git_url("pkg:oci/nginx?repository_url=docker.io/library/nginx")
        is False
    )


def test_find_host_config_longest_path_match():
    """_find_host_config should prefer the host whose URL path is the longest prefix match."""
    hosts = {
        "broad": {"url": "https://gitlab.com", "type": "gitlab"},
        "org": {"url": "https://gitlab.com/myorg", "type": "gitlab"},
        "team": {"url": "https://gitlab.com/myorg/team", "type": "gitlab"},
        "other": {"url": "https://other.com/foo", "type": "gitlab"},
    }

    # Exact org match
    name, config = CloudMap._find_host_config(hosts, "gitlab.com", "myorg/repo")
    assert name == "org"

    # Deeper path matches team
    name, config = CloudMap._find_host_config(hosts, "gitlab.com", "myorg/team/repo")
    assert name == "team"

    # Path not under any org falls back to broad
    name, config = CloudMap._find_host_config(hosts, "gitlab.com", "other-org/repo")
    assert name == "broad"

    # No hostname match
    name, config = CloudMap._find_host_config(hosts, "example.com", "foo")
    assert config is None

    # No path given — broad wins as fallback
    name, config = CloudMap._find_host_config(hosts, "gitlab.com")
    assert name == "broad"

    # Different host entirely
    name, config = CloudMap._find_host_config(hosts, "other.com", "foo/bar")
    assert name == "other"

    # Single host, no path in config — always matches
    single = {"only": {"url": "https://gitlab.com", "type": "gitlab"}}
    name, config = CloudMap._find_host_config(single, "gitlab.com", "anything/here")
    assert name == "only"


# fmt: off
expected_full_graph = """\
CloudMap
├── Repositories
│   ├── Repository git://unfurl.cloud/feb20a/dashboard.git
│   │   └── contains
│   │       ├── Artifact git://unfurl.cloud/feb20a/dashboard.git#:.gitlab-ci.yml
│   │       │   │        (cloudmap.artifacts.GitLabPipeline)
│   │       ├── Artifact git://unfurl.cloud/feb20a/dashboard.git#:ensemble/ensemble.yaml
│   │       │   │        (cloudmap.artifacts.unfurl.Ensemble)
│   │       │   └── references
│   │       │       ├── Artifact git://unfurl.cloud/onecommons/std.git#v1.1.1:.
│   │       │       │   │        (cloudmap.artifacts.unfurl.Package v1.1.1)
│   │       │       └── Repository git://unfurl.cloud/feb20a/dashboard.git [seen]
│   │       └── Instantiation git://unfurl.cloud/feb20a/dashboard.git#:environments/aws/onecommons/blueprints/odoo/odoo-aws-1/ensemble.yaml
│   │           │             (cloudmap.artifacts.unfurl.Ensemble)
│   │           ├── source
│   │           │   └── Artifact git://unfurl.cloud/onecommons/blueprints/odoo.git#:ensemble-template.yaml%23spec/service_template
│   │           │       │        Odoo (cloudmap.artifacts.tosca.ServiceTemplate v0.1)
│   │           │       ├── references
│   │           │       │   ├── Artifact git://unfurl.cloud/onecommons/unfurl-types#v0.7.7:.
│   │           │       │   │   │        (cloudmap.artifacts.unfurl.Package v0.7.7)
│   │           │       │   ├── Artifact pkg:oci/odoo?repository_url=docker.io/bitnami/odoo&tag=latest
│   │           │       │   │   │        (cloudmap.artifacts.oci.Image)
│   │           │       │   └── Repository git://unfurl.cloud/onecommons/blueprints/odoo.git
│   │           │       │       └── contains
│   │           │       │           ├── Artifact git://unfurl.cloud/onecommons/blueprints/odoo.git#:.gitlab-ci.yml
│   │           │       │           │   │        (cloudmap.artifacts.GitLabPipeline)
│   │           │       │           ├── Artifact git://unfurl.cloud/onecommons/blueprints/odoo.git#:ensemble-template.yaml%23spec/service_template
│   │           │       │           │   │        Odoo (cloudmap.artifacts.tosca.ServiceTemplate v0.1, tosca.artifacts.File) [seen]
│   │           │       │           └── Artifact git://unfurl.cloud/onecommons/blueprints/odoo.git#:unfurl.yaml
│   │           │       │               │        (cloudmap.artifacts.unfurl.Project)
│   │           │       ├── dependencies
│   │           │       │   ├── Database: PostgresDB@unfurl.cloud/onecommons/unfurl-types
│   │           │       │   │   └── contains
│   │           │       │   │       ├── Artifact git://unfurl.cloud/onecommons/unfurl-types.git#:.gitlab-ci.yml
│   │           │       │   │       │   │        (cloudmap.artifacts.GitLabPipeline)
│   │           │       │   │       └── Artifact git://unfurl.cloud/onecommons/unfurl-types.git#:dummy-ensemble.yaml
│   │           │       │   │           │        (cloudmap.artifacts.tosca.TypeLibrary)
│   │           │       │   ├── aws: unfurl.relationships.ConnectsTo.AWSAccount
│   │           │       │   │   └── extends
│   │           │       │   │       └── unfurl.relationships.ConnectsTo.CloudAccount
│   │           │       │   └── gcp: unfurl.relationships.ConnectsTo.GoogleCloudProject
│   │           │       │       └── extends
│   │           │       │           └── unfurl.relationships.ConnectsTo.CloudAccount
│   │           │       └── instantiates
│   │           │           └── the_app: Odoo@unfurl.cloud/onecommons/blueprints/odoo
│   │           │               └── contains
│   │           │                   ├── Artifact git://unfurl.cloud/onecommons/blueprints/odoo.git#:.gitlab-ci.yml
│   │           │                   │   │        (cloudmap.artifacts.GitLabPipeline) [seen]
│   │           │                   ├── Artifact git://unfurl.cloud/onecommons/blueprints/odoo.git#:ensemble-template.yaml%23spec/service_template
│   │           │                   │   │        Odoo (cloudmap.artifacts.tosca.ServiceTemplate v0.1, tosca.artifacts.File) [seen]
│   │           │                   └── Artifact git://unfurl.cloud/onecommons/blueprints/odoo.git#:unfurl.yaml
│   │           │                       │        (cloudmap.artifacts.unfurl.Project) [seen]
│   │           ├── instantiated
│   │           │   └── Service https://example.com/oodo
│   │           │       │       (Odoo@unfurl.cloud/onecommons/blueprints/odoo)
│   │           │       └── instantiated_by
│   │           │           └── Instantiation git://unfurl.cloud/feb20a/dashboard.git#:environments/aws/onecommons/blueprints/odoo/odoo-aws-1/ensemble.yaml
│   │           │               │             (cloudmap.artifacts.unfurl.Ensemble) [seen]
│   │           └── inputs
│   │               ├── Artifact git://unfurl.cloud/onecommons/std.git#v1.1.1:.
│   │               │   │        (cloudmap.artifacts.unfurl.Package v1.1.1) [seen]
│   │               └── Artifact pkg:oci/odoo?repository_url=docker.io/bitnami/odoo&tag=latest
│   │                   │        (cloudmap.artifacts.oci.Image) [seen]
│   ├── Repository git://unfurl.cloud/onecommons/blueprints/odoo.git [seen]
│   ├── Repository git://unfurl.cloud/onecommons/std.git
│   │   └── contains
│   │       ├── Artifact git://unfurl.cloud/onecommons/std.git#:.devcontainer/Containerfile
│   │       │   │        (cloudmap.artifacts.Containerfile)
│   │       ├── Artifact git://unfurl.cloud/onecommons/std.git#:.gitlab-ci.yml
│   │       │   │        (cloudmap.artifacts.GitLabPipeline)
│   │       └── Artifact git://unfurl.cloud/onecommons/std.git#:dummy-ensemble.yaml
│   │           │        (cloudmap.artifacts.tosca.TypeLibrary)
│   └── Repository git://unfurl.cloud/onecommons/unfurl-types.git
│       └── contains
│           ├── Artifact git://unfurl.cloud/onecommons/unfurl-types.git#:.gitlab-ci.yml
│           │   │        (cloudmap.artifacts.GitLabPipeline) [seen]
│           └── Artifact git://unfurl.cloud/onecommons/unfurl-types.git#:dummy-ensemble.yaml
│               │        (cloudmap.artifacts.tosca.TypeLibrary) [seen]
├── Artifacts
│   ├── Artifact git://unfurl.cloud/feb20a/dashboard.git#:.gitlab-ci.yml
│   │   │        (cloudmap.artifacts.GitLabPipeline) [seen]
│   ├── Artifact git://unfurl.cloud/feb20a/dashboard.git#:ensemble/ensemble.yaml
│   │   │        (cloudmap.artifacts.unfurl.Ensemble) [seen]
│   ├── Artifact git://unfurl.cloud/feb20a/dashboard.git#:environments/aws/onecommons/blueprints/odoo/odoo-aws-1/ensemble.yaml
│   │   │        Odoo (cloudmap.artifacts.unfurl.Ensemble v0.1) [seen]
│   ├── Artifact git://unfurl.cloud/onecommons/blueprints/odoo.git#:.gitlab-ci.yml
│   │   │        (cloudmap.artifacts.GitLabPipeline) [seen]
│   ├── Artifact git://unfurl.cloud/onecommons/blueprints/odoo.git#:ensemble-template.yaml%23spec/service_template
│   │   │        Odoo (cloudmap.artifacts.tosca.ServiceTemplate v0.1) [seen]
│   ├── Artifact git://unfurl.cloud/onecommons/std.git#:.devcontainer/Containerfile
│   │   │        (cloudmap.artifacts.Containerfile) [seen]
│   ├── Artifact git://unfurl.cloud/onecommons/std.git#:.gitlab-ci.yml
│   │   │        (cloudmap.artifacts.GitLabPipeline) [seen]
│   ├── Artifact git://unfurl.cloud/onecommons/std.git#:dummy-ensemble.yaml
│   │   │        (cloudmap.artifacts.tosca.TypeLibrary) [seen]
│   ├── Artifact git://unfurl.cloud/onecommons/unfurl-types.git#:.gitlab-ci.yml
│   │   │        (cloudmap.artifacts.GitLabPipeline) [seen]
│   ├── Artifact git://unfurl.cloud/onecommons/unfurl-types.git#:dummy-ensemble.yaml
│   │   │        (cloudmap.artifacts.tosca.TypeLibrary) [seen]
│   └── Artifact pkg:oci/odoo?repository_url=docker.io/bitnami/odoo&tag=latest
│       │        (cloudmap.artifacts.oci.Image) [seen]
├── Instantiations
│   └── Instantiation git://unfurl.cloud/feb20a/dashboard.git#:environments/aws/onecommons/blueprints/odoo/odoo-aws-1/ensemble.yaml
│       │             (cloudmap.artifacts.unfurl.Ensemble) [seen]
├── Services
│   └── Service https://example.com/oodo
│       │       (Odoo@unfurl.cloud/onecommons/blueprints/odoo) [seen]
└── Types
    ├── Type Odoo@unfurl.cloud/onecommons/blueprints/odoo [seen]
    ├── Type unfurl.relationships.ConnectsTo.AWSAccount [seen]
    └── Type unfurl.relationships.ConnectsTo.GoogleCloudProject [seen]
"""

expected_artifact_graph = """\
Artifact git://unfurl.cloud/onecommons/blueprints/odoo.git#:ensemble-template.yaml%23spec/service_template
│        Odoo (cloudmap.artifacts.tosca.ServiceTemplate v0.1)
├── references
│   ├── Artifact git://unfurl.cloud/onecommons/unfurl-types#v0.7.7:.
│   │   │        (cloudmap.artifacts.unfurl.Package v0.7.7)
│   ├── Artifact pkg:oci/odoo?repository_url=docker.io/bitnami/odoo&tag=latest
│   │   │        (cloudmap.artifacts.oci.Image)
│   └── Repository git://unfurl.cloud/onecommons/blueprints/odoo.git
│       └── contains
│           ├── Artifact git://unfurl.cloud/onecommons/blueprints/odoo.git#:.gitlab-ci.yml
│           │   │        (cloudmap.artifacts.GitLabPipeline)
│           ├── Artifact git://unfurl.cloud/onecommons/blueprints/odoo.git#:ensemble-template.yaml%23spec/service_template
│           │   │        Odoo (cloudmap.artifacts.tosca.ServiceTemplate v0.1, tosca.artifacts.File) [seen]
│           └── Artifact git://unfurl.cloud/onecommons/blueprints/odoo.git#:unfurl.yaml
│               │        (cloudmap.artifacts.unfurl.Project)
├── dependencies
│   ├── Database: PostgresDB@unfurl.cloud/onecommons/unfurl-types
│   │   └── contains
│   │       ├── Artifact git://unfurl.cloud/onecommons/unfurl-types.git#:.gitlab-ci.yml
│   │       │   │        (cloudmap.artifacts.GitLabPipeline)
│   │       └── Artifact git://unfurl.cloud/onecommons/unfurl-types.git#:dummy-ensemble.yaml
│   │           │        (cloudmap.artifacts.tosca.TypeLibrary)
│   ├── aws: unfurl.relationships.ConnectsTo.AWSAccount
│   │   └── extends
│   │       └── unfurl.relationships.ConnectsTo.CloudAccount
│   └── gcp: unfurl.relationships.ConnectsTo.GoogleCloudProject
│       └── extends
│           └── unfurl.relationships.ConnectsTo.CloudAccount
└── instantiates
    └── the_app: Odoo@unfurl.cloud/onecommons/blueprints/odoo
        └── contains
            ├── Artifact git://unfurl.cloud/onecommons/blueprints/odoo.git#:.gitlab-ci.yml
            │   │        (cloudmap.artifacts.GitLabPipeline) [seen]
            ├── Artifact git://unfurl.cloud/onecommons/blueprints/odoo.git#:ensemble-template.yaml%23spec/service_template
            │   │        Odoo (cloudmap.artifacts.tosca.ServiceTemplate v0.1, tosca.artifacts.File) [seen]
            └── Artifact git://unfurl.cloud/onecommons/blueprints/odoo.git#:unfurl.yaml
                │        (cloudmap.artifacts.unfurl.Project) [seen]
"""

# URL that exists as both an Artifact and an Instantiation -- instantiation tree first
expected_dual_record_graph = """\
Instantiation git://unfurl.cloud/feb20a/dashboard.git#:environments/aws/onecommons/blueprints/odoo/odoo-aws-1/ensemble.yaml
│             (cloudmap.artifacts.unfurl.Ensemble)
├── source
│   └── Artifact git://unfurl.cloud/onecommons/blueprints/odoo.git#:ensemble-template.yaml%23spec/service_template
│       │        Odoo (cloudmap.artifacts.tosca.ServiceTemplate v0.1)
│       ├── references
│       │   ├── Artifact git://unfurl.cloud/onecommons/unfurl-types#v0.7.7:.
│       │   │   │        (cloudmap.artifacts.unfurl.Package v0.7.7)
│       │   ├── Artifact pkg:oci/odoo?repository_url=docker.io/bitnami/odoo&tag=latest
│       │   │   │        (cloudmap.artifacts.oci.Image)
│       │   └── Repository git://unfurl.cloud/onecommons/blueprints/odoo.git
│       │       └── contains
│       │           ├── Artifact git://unfurl.cloud/onecommons/blueprints/odoo.git#:.gitlab-ci.yml
│       │           │   │        (cloudmap.artifacts.GitLabPipeline)
│       │           ├── Artifact git://unfurl.cloud/onecommons/blueprints/odoo.git#:ensemble-template.yaml%23spec/service_template
│       │           │   │        Odoo (cloudmap.artifacts.tosca.ServiceTemplate v0.1, tosca.artifacts.File) [seen]
│       │           └── Artifact git://unfurl.cloud/onecommons/blueprints/odoo.git#:unfurl.yaml
│       │               │        (cloudmap.artifacts.unfurl.Project)
│       ├── dependencies
│       │   ├── Database: PostgresDB@unfurl.cloud/onecommons/unfurl-types
│       │   │   └── contains
│       │   │       ├── Artifact git://unfurl.cloud/onecommons/unfurl-types.git#:.gitlab-ci.yml
│       │   │       │   │        (cloudmap.artifacts.GitLabPipeline)
│       │   │       └── Artifact git://unfurl.cloud/onecommons/unfurl-types.git#:dummy-ensemble.yaml
│       │   │           │        (cloudmap.artifacts.tosca.TypeLibrary)
│       │   ├── aws: unfurl.relationships.ConnectsTo.AWSAccount
│       │   │   └── extends
│       │   │       └── unfurl.relationships.ConnectsTo.CloudAccount
│       │   └── gcp: unfurl.relationships.ConnectsTo.GoogleCloudProject
│       │       └── extends
│       │           └── unfurl.relationships.ConnectsTo.CloudAccount
│       └── instantiates
│           └── the_app: Odoo@unfurl.cloud/onecommons/blueprints/odoo
│               └── contains
│                   ├── Artifact git://unfurl.cloud/onecommons/blueprints/odoo.git#:.gitlab-ci.yml
│                   │   │        (cloudmap.artifacts.GitLabPipeline) [seen]
│                   ├── Artifact git://unfurl.cloud/onecommons/blueprints/odoo.git#:ensemble-template.yaml%23spec/service_template
│                   │   │        Odoo (cloudmap.artifacts.tosca.ServiceTemplate v0.1, tosca.artifacts.File) [seen]
│                   └── Artifact git://unfurl.cloud/onecommons/blueprints/odoo.git#:unfurl.yaml
│                       │        (cloudmap.artifacts.unfurl.Project) [seen]
├── instantiated
│   └── Service https://example.com/oodo
│       │       (Odoo@unfurl.cloud/onecommons/blueprints/odoo)
│       └── instantiated_by
│           └── Instantiation git://unfurl.cloud/feb20a/dashboard.git#:environments/aws/onecommons/blueprints/odoo/odoo-aws-1/ensemble.yaml
│               │             (cloudmap.artifacts.unfurl.Ensemble) [seen]
└── inputs
    ├── Artifact git://unfurl.cloud/onecommons/std.git#v1.1.1:.
    │   │        (cloudmap.artifacts.unfurl.Package v1.1.1)
    └── Artifact pkg:oci/odoo?repository_url=docker.io/bitnami/odoo&tag=latest
        │        (cloudmap.artifacts.oci.Image) [seen]
Artifact git://unfurl.cloud/feb20a/dashboard.git#:environments/aws/onecommons/blueprints/odoo/odoo-aws-1/ensemble.yaml
│        Odoo (cloudmap.artifacts.unfurl.Ensemble v0.1)
├── references
│   ├── Artifact git://unfurl.cloud/onecommons/std.git#v1.1.1:.
│   │   │        (cloudmap.artifacts.unfurl.Package v1.1.1) [seen]
│   ├── Artifact pkg:oci/odoo?repository_url=docker.io/bitnami/odoo&tag=latest
│   │   │        (cloudmap.artifacts.oci.Image) [seen]
│   └── Repository git://unfurl.cloud/feb20a/dashboard.git
│       └── contains
│           ├── Artifact git://unfurl.cloud/feb20a/dashboard.git#:.gitlab-ci.yml
│           │   │        (cloudmap.artifacts.GitLabPipeline)
│           ├── Artifact git://unfurl.cloud/feb20a/dashboard.git#:ensemble/ensemble.yaml
│           │   │        (cloudmap.artifacts.unfurl.Ensemble)
│           │   └── references
│           │       ├── Artifact git://unfurl.cloud/onecommons/std.git#v1.1.1:.
│           │       │   │        (cloudmap.artifacts.unfurl.Package v1.1.1) [seen]
│           │       └── Repository git://unfurl.cloud/feb20a/dashboard.git [seen]
│           └── Instantiation git://unfurl.cloud/feb20a/dashboard.git#:environments/aws/onecommons/blueprints/odoo/odoo-aws-1/ensemble.yaml
│               │             (cloudmap.artifacts.unfurl.Ensemble) [seen]
├── dependencies
│   ├── aws: unfurl.relationships.ConnectsTo.AWSAccount
│   ├── gcp: unfurl.relationships.ConnectsTo.GoogleCloudProject
│   └── odoo-aws-1: unfurl.relationships.ConnectsTo.AWSAccount
└── instantiates
    └── the_app: Odoo@unfurl.cloud/onecommons/blueprints/odoo
"""
# fmt: on


def test_cloudmap_graph(tmp_path):
    """Test cloudmap_graph_console renders the expected tree from expected_cloudmap."""
    cloudmap_path = tmp_path / "cloudmap.yaml"
    cloudmap_path.write_text(expected_cloudmap)
    db = CloudMapDB(str(cloudmap_path))

    assert _capture_graph(db) == expected_full_graph

    assert (
        _capture_graph(
            db,
            "git://unfurl.cloud/onecommons/blueprints/odoo.git#:ensemble-template.yaml%23spec/service_template",
        )
        == expected_artifact_graph
    )

    # URL present in both artifacts and instantiations — shows both trees
    assert (
        _capture_graph(
            db,
            "git://unfurl.cloud/feb20a/dashboard.git#:environments/aws/onecommons/blueprints/odoo/odoo-aws-1/ensemble.yaml",
        )
        == expected_dual_record_graph
    )

    assert "Record not found" in _capture_graph(db, "nonexistent://url")


def test_cloudmap_graph_json(tmp_path):
    """Test cloudmap_graph_json returns the expected JSON structure."""
    import json
    from pathlib import Path

    from unfurl.reporting import cloudmap_graph_json

    cloudmap_path = tmp_path / "cloudmap.yaml"
    cloudmap_path.write_text(expected_cloudmap)
    db = CloudMapDB(str(cloudmap_path))

    # Single artifact query
    fixture_dir = Path(__file__).parent / "fixtures"
    result = cloudmap_graph_json(
        db,
        "git://unfurl.cloud/onecommons/blueprints/odoo.git#:ensemble-template.yaml%23spec/service_template",
    )
    expected_artifact = json.loads(
        (fixture_dir / "cloudmap_graph_artifact.json").read_text()
    )
    assert result == expected_artifact

    # URL present in both artifacts and instantiations — shows both trees
    dual_result = cloudmap_graph_json(
        db,
        "git://unfurl.cloud/feb20a/dashboard.git#:environments/aws/onecommons/blueprints/odoo/odoo-aws-1/ensemble.yaml",
    )
    expected_dual = json.loads((fixture_dir / "cloudmap_graph_dual.json").read_text())
    assert dual_result == expected_dual

    # Not found
    assert cloudmap_graph_json(db, "nonexistent://url") == {
        "error": "Record not found: nonexistent://url",
    }

    # Full graph: compare against saved fixture
    full = cloudmap_graph_json(db)
    expected_full = json.loads((fixture_dir / "cloudmap_graph.json").read_text())
    assert full == expected_full


# ---------------------------------------------------------------------------
# GET /cloudmap/facets — mirrors the Rust integration tests in
# rust/server/tests/test_cloudmap.rs (facets_*), on the same fixture
# plus the same seeded records, so the two servers' expectations stay in
# lock-step.
# ---------------------------------------------------------------------------


def _facet_seeds():
    """The controlled dataset the facet tests add on top of the fixture —
    keep in lock-step with ``seed_facet_records`` in
    rust/server/tests/test_cloudmap.rs."""
    types = {
        "FacetBase": {"name": "FacetBase", "extends": ["FacetBase"]},
        "FacetDerived": {
            "name": "FacetDerived",
            "extends": ["FacetDerived", "FacetBase"],
        },
    }
    artifacts = {
        "facet:a1": {
            "type": {"FacetDerived": {}},
            "metadata": {
                "topics": ["db", "web"],
                "platforms": [
                    {"os": "linux", "architecture": "amd64"},
                    {"os": "windows", "architecture": "arm64"},
                ],
            },
        },
        "facet:a2": {
            "type": {"FacetBase": {}},
            "metadata": {"topics": ["db", "db"]},
        },
        "facet:a3": {
            "type": {"FacetDerived": {}},
            "metadata": {
                "topics": ["web"],
                # same platform as a1's first, spelled in the other key
                # order: must land in the same canonical bucket
                "platforms": [{"architecture": "amd64", "os": "linux"}],
            },
        },
        "facet:a4": {"metadata": {"name": "quiet"}},
    }
    return types, artifacts


@pytest.fixture
def facet_test_client():
    """`cloudmap_test_client` with the facet seed records injected."""
    from unfurl.server.serve import app

    cloudmap_doc = _load_cloudmap_fixture()
    types, artifacts = _facet_seeds()
    cloudmap_doc.setdefault("types", {}).update(types)
    cloudmap_doc.setdefault("artifacts", {}).update(artifacts)
    with patch("unfurl.server.cache.load_yaml_from_cache") as mock_load_yaml:
        mock_load_yaml.return_value = (None, cloudmap_doc)
        with app.test_client() as client:
            yield client


def test_cloudmap_facets_group_by_topics(facet_test_client):
    resp = facet_test_client.get(
        "/cloudmap/facets?kind=artifacts&group_by=metadata/topics"
    )
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["meta"] == {
        "group_by": "/metadata/topics",
        "facets": [],
        "subtypes": False,  # no type column, so no rollup was applied
    }
    # 11 fixture artifacts + 4 seeded
    assert body["total"] == 15
    # a2's duplicate "db" counts once; no `facets` key without facet columns
    assert body["groups"] == {"db": {"count": 2}, "web": {"count": 2}}


def test_cloudmap_facets_subtypes_invariant(facet_test_client):
    # The rollup invariant: every type's bucket equals what ?type=T selects.
    resp = facet_test_client.get("/cloudmap/facets?kind=artifacts&group_by=type")
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["meta"]["subtypes"] is True
    groups = body["groups"]
    assert "FacetBase" in groups, groups
    from urllib.parse import quote

    for type_name, entry in groups.items():
        selected = facet_test_client.get(
            f"/cloudmap?kind=artifacts&type={quote(type_name)}"
        )
        assert selected.status_code == 200
        result = selected.get_json()["result"]
        count = sum(
            len(records) for records in result.values() if isinstance(records, dict)
        )
        assert entry["count"] == count, (
            f"bucket for {type_name!r} must count exactly what ?type= selects"
        )

    # subtypes=false counts exact declared names only
    resp = facet_test_client.get(
        "/cloudmap/facets?kind=artifacts&group_by=type&subtypes=false"
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["meta"]["subtypes"] is False
    assert body["groups"]["FacetBase"]["count"] == 1
    assert body["groups"]["FacetDerived"]["count"] == 2


def test_cloudmap_facets_repeated_and_composite(facet_test_client):
    # Two columns — a composite (type × platforms) and a simple one — via
    # the repeated `facet=` spelling. This is also the guard for the
    # getlist workaround: APIFlask's pydantic adapter binds only the
    # FIRST value of a repeated query key, so if the handler ever reads
    # the bound model instead of request.args.getlist, the second
    # column vanishes and the meta assertion fails.
    resp = facet_test_client.get(
        "/cloudmap/facets?kind=artifacts&group_by=metadata/topics"
        "&facet=type,metadata/platforms&facet=type&subtypes=false"
    )
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["meta"]["facets"] == [["/type", "/metadata/platforms"], ["/type"]]
    linux = '["FacetDerived",{"architecture":"amd64","os":"linux"}]'
    windows = '["FacetDerived",{"architecture":"arm64","os":"windows"}]'
    # a1's and a3's differently-spelled platforms merge into one
    # canonical bucket; a2 (no platforms) is absent from the composite
    # column but present in the simple one
    assert body["groups"]["web"] == {
        "count": 2,
        "facets": [{linux: 2, windows: 1}, {"FacetDerived": 2}],
    }
    assert body["groups"]["db"] == {
        "count": 2,
        "facets": [{linux: 1, windows: 1}, {"FacetBase": 1, "FacetDerived": 1}],
    }


def test_cloudmap_facets_error_statuses(facet_test_client):
    # Missing required group_by: schema-level, 422 from APIFlask.
    resp = facet_test_client.get("/cloudmap/facets?kind=artifacts")
    assert resp.status_code == 422

    # Empty path segment: semantic, 400.
    resp = facet_test_client.get("/cloudmap/facets?group_by=metadata//topics")
    assert resp.status_code == 400

    # Bad facet member path: 400 too.
    resp = facet_test_client.get("/cloudmap/facets?group_by=type&facet=a//b")
    assert resp.status_code == 400

    # Unknown kind: 422 here (the pydantic Literal rejects it), where the
    # rust handler 404s — the same pre-existing divergence GET /cloudmap
    # has, so the two endpoints at least fail alike per server.
    resp = facet_test_client.get("/cloudmap/facets?kind=nope&group_by=type")
    assert resp.status_code == 422


def test_cloudmap_endpoint_repeated_filters(cloudmap_test_client):
    # `filter=` repeats: every occurrence must match. This is also the
    # getlist guard for get_cloudmap — the pydantic adapter binds only
    # the first value of a repeated key, so if the handler read the
    # bound model the second filter would be dropped and the exclusion
    # case below would wrongly return the dashboard repo.
    from urllib.parse import quote

    both = (
        "/cloudmap?filter="
        + quote("/private=true")
        + "&filter="
        + quote("/branches/main=4551885dfab39991cfdb958cb79fcb6aa282481d")
    )
    resp = cloudmap_test_client.get(both)
    assert resp.status_code == 200
    assert list(resp.get_json()["result"]["repositories"]) == [
        "git://unfurl.cloud/feb20a/dashboard.git"
    ]

    # Each filter matches a record on its own, but no record matches both.
    exclusive = (
        "/cloudmap?filter="
        + quote("/private=true")
        + "&filter="
        + quote(
            "/metadata/homepage_url=https://unfurl.cloud/onecommons/blueprints/odoo"
        )
    )
    resp = cloudmap_test_client.get(exclusive)
    assert resp.status_code == 200
    assert resp.get_json()["result"] == {}


def test_cloudmap_facets_repeated_filters(facet_test_client):
    # The facets handler has its own getlist call; the same exclusion
    # shape guards it.
    from urllib.parse import quote

    resp = facet_test_client.get(
        "/cloudmap/facets?kind=artifacts&group_by=metadata/topics"
        + "&filter="
        + quote("/metadata/topics=db")
        + "&filter="
        + quote("/metadata/topics=web")
    )
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    # only a1 carries both topics; its other values still fan out
    assert body["total"] == 1
    assert body["groups"] == {"db": {"count": 1}, "web": {"count": 1}}


def test_cloudmap_facets_non_ascii_canonical_keys():
    # Canonical keys must carry non-ASCII characters raw, not as \uXXXX
    # escapes: json.dumps defaults to ensure_ascii=True, which would
    # spell this key "café" while the rust server emits "café" —
    # the same literal asserted in facets_non_ascii_canonical_keys in
    # rust/server/tests/test_cloudmap.rs, which is what pins the two
    # implementations to one spelling.
    from unfurl.server.serve import app

    doc = _load_cloudmap_fixture()
    doc.setdefault("artifacts", {})["facet:unicode"] = {
        "type": {"FacetBase": {}},
        "metadata": {"platforms": [{"os": "linux", "variant": "café"}]},
    }
    with patch("unfurl.server.cache.load_yaml_from_cache") as mock_load_yaml:
        mock_load_yaml.return_value = (None, doc)
        with app.test_client() as client:
            resp = client.get(
                "/cloudmap/facets?kind=artifacts&group_by=metadata/platforms"
            )
            assert resp.status_code == 200, resp.get_json()
            body = resp.get_json()
            assert body["groups"] == {'{"os":"linux","variant":"café"}': {"count": 1}}


def test_cloudmap_facets_missing_group_path(facet_test_client):
    resp = facet_test_client.get("/cloudmap/facets?group_by=no/such/path")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["groups"] == {}
    # records without the path still count toward total
    assert body["total"] > 0


def _host_repo(url: str, internal_id: str = "7", **kw) -> Repository:
    return Repository(url=url, path=url.split("/", 3)[-1], internal_id=internal_id, **kw)


class _SyncSession:
    """Imports repositories the way one host sync does: through the host's own
    import method, reusing one host and directory, with only the host API
    conversion stubbed out."""

    def __init__(self, manager_cls, db: CloudMapDB) -> None:
        self.manager_cls = manager_cls
        self.manager = manager_cls.__new__(manager_cls)
        self.manager.logger = Mock()
        self.directory = Mock()
        self.directory.context = db

    def import_repo(self, repo_info: Repository) -> None:
        if self.manager_cls is GitlabManager:
            with patch.object(
                GitlabManager, "gitlab_project_to_repository", return_value=repo_info
            ):
                self.manager._import_project(Mock(), self.directory, False)
        else:
            with patch.object(
                GithubManager, "github_repository_to_repository", return_value=repo_info
            ):
                self.manager._import_repository(
                    Mock(full_name="x"), self.directory, False
                )


@pytest.mark.parametrize("manager_cls", [GitlabManager, GithubManager])
def test_sync_records_moved_repository(manager_cls):
    old_url = "git://example.com/team/old.git"
    new_url = "git://example.com/team/new.git"
    db = CloudMapDB("", contents={}, validate=False)
    db.add_record(_host_repo(old_url))
    # same id on another host is a different repository
    db.add_record(_host_repo("git://other.com/team/old.git"))
    sync = _SyncSession(manager_cls, db)

    sync.import_repo(_host_repo(new_url))
    old = db.get_repository(old_url)
    assert old and (old.status, old.moved_to) == ("moved", new_url)
    other = db.get_repository("git://other.com/team/old.git")
    assert other and other.status is None
    new = db.get_repository(new_url)
    assert new and new.status is None

    # moved back: the record it left behind is marked instead
    sync.import_repo(_host_repo(old_url))
    new = db.get_repository(new_url)
    assert new and (new.status, new.moved_to) == ("moved", old_url)
    old = db.get_repository(old_url)
    assert old and (old.status, old.moved_to) == (None, None)


def test_sync_without_internal_id_records_no_move():
    old_url = "git://example.com/team/old.git"
    db = CloudMapDB("", contents={}, validate=False)
    db.add_record(_host_repo(old_url))
    _SyncSession(GitlabManager, db).import_repo(
        _host_repo("git://example.com/team/new.git", internal_id="")
    )
    old = db.get_repository(old_url)
    assert old and old.status is None


def test_resync_records_no_move():
    url = "git://example.com/team/repo.git"
    db = CloudMapDB("", contents={}, validate=False)
    db.add_record(_host_repo(url))
    _SyncSession(GitlabManager, db).import_repo(_host_repo(url))
    repo = db.get_repository(url)
    assert repo and (repo.status, repo.moved_to) == (None, None)


def test_graph_walk_follows_moved_to():
    from unfurl.reporting import CollectVisitor, walk_cloudmap_graph_from

    old_url = "git://example.com/team/old.git"
    new_url = "git://example.com/team/new.git"
    db = CloudMapDB("", contents={}, validate=False)
    db.add_record(_host_repo(old_url, status="moved", moved_to=new_url))
    db.add_record(_host_repo(new_url))
    visitor = CollectVisitor({old_url}, limit=10)
    walk_cloudmap_graph_from(db, visitor, [old_url])
    assert list(visitor.result.get("repositories", {})) == [new_url]


def test_analyze_endpoint_url_guard():
    from unfurl.server.cloudmap import _analyzable_url

    assert _analyzable_url("https://example.com/app") == "https://example.com/app"
    assert _analyzable_url("git://example.com/repo.git") == "git://example.com/repo.git"
    # a bare name is a container image, never a local path
    assert _analyzable_url("/etc").startswith("pkg:oci/")
    assert _analyzable_url("library/nginx").startswith("pkg:oci/nginx")
    for url in ("file:///etc", "git-local://abc:/x", "git+file:///srv/repo"):
        assert _analyzable_url(url) is None, url


def test_analyze_endpoint_changed_sections():
    from unfurl.server.cloudmap import _changed_sections

    before = {"services": {"a": {"x": 1}, "b": {"x": 2}}, "types": {"T": {}}}
    after = {"services": {"a": {"x": 1}, "b": {"x": 3}, "c": {}}, "types": {}}
    assert _changed_sections(before, after) == {
        "services": {"b": {"x": 3}, "c": {}},
        "types": {"T": {"unfurl.server.deleted": True}},
    }


class _GitlabHostFixture:
    """A cloudmap whose GitLab host keys records under a canonical url that
    differs from the urls GitLab itself reports."""

    CANONICAL = "git://unfurl.cloud/group/project.git"
    # what a GitLab instance with canonical_url https://unfurl.cloud sends
    SENT = "http://gdk.test:3000/group/project.git"

    def _cloudmap(self, db: CloudMapDB, with_host: bool = True):
        cloud_map = CloudMap(None, "", db=db)
        if with_host:
            cloud_map.local_env = Mock()
        manager = GitlabManager.__new__(GitlabManager)
        manager.gitlab = Mock()
        manager.hostname = "gdk.test"
        manager.canonical_url = "https://unfurl.cloud"
        manager.logger = Mock()
        return cloud_map, manager

    def _record(self, description: str, **kw) -> Repository:
        return Repository(
            url=self.CANONICAL,
            path="group/project",
            name="project",
            metadata=RepositoryMetadata(description=description),
            **kw,
        )

    def _db_with_existing(self) -> CloudMapDB:
        db = CloudMapDB("", contents={}, validate=False)
        existing = self._record("old")
        existing.contains = TypeRefs.urls_fromdict(
            {"ensemble.yaml": None}, keys_are_urls=True
        )
        db.add_record(existing)
        return db


class TestAnalyzeMetadata(_GitlabHostFixture):
    """``CloudMap.analyze_url(url, "metadata")`` refreshes a repository record
    from its host without cloning it."""

    def _analyze(self, db: CloudMapDB, fresh: Repository, with_host: bool = True):
        cloud_map, manager = self._cloudmap(db, with_host)
        with patch.object(CloudMap, "get_host", return_value=manager), patch.object(
            GitlabManager, "gitlab_project_to_repository", return_value=fresh
        ):
            return cloud_map.analyze_url(self.SENT, "metadata")

    def test_refreshes_existing_record_and_keeps_contains(self):
        db = self._db_with_existing()
        record = self._analyze(db, self._record("new", status="archived"))
        assert record is not None and record.key == self.CANONICAL
        stored = db.get_repository(self.CANONICAL)
        assert stored and stored.metadata.description == "new"
        assert stored.status == "archived"
        assert ("", "ensemble.yaml") in stored.contains

    def test_unchanged_record_is_skipped(self):
        db = self._db_with_existing()
        before = db.get_repository(self.CANONICAL)
        assert self._analyze(db, self._record("old")) is None
        assert db.get_repository(self.CANONICAL) is before

    def test_new_url_is_added(self):
        db = CloudMapDB("", contents={}, validate=False)
        record = self._analyze(db, self._record("new"))
        assert record is not None
        assert db.get_repository(self.CANONICAL)

    def test_without_a_host_the_record_is_left_alone(self):
        db = self._db_with_existing()
        before = db.get_repository(self.CANONICAL)
        assert self._analyze(db, self._record("new"), with_host=False) is None
        assert db.get_repository(self.CANONICAL) is before

    def test_host_without_the_project_leaves_the_record_alone(self):
        db = self._db_with_existing()
        before = db.get_repository(self.CANONICAL)
        cloud_map, manager = self._cloudmap(db)
        with patch.object(CloudMap, "get_host", return_value=manager), patch.object(
            GitlabManager, "import_project_url", return_value=None
        ):
            assert cloud_map.analyze_url(self.SENT, "metadata") is None
        assert db.get_repository(self.CANONICAL) is before


class TestUpdateRepository(_GitlabHostFixture):
    """``CloudMap.update_repository`` records what happened to a repository
    without contacting its host, under the key the host gives it."""

    def _update(self, db: CloudMapDB, url: str, **kw):
        cloud_map, manager = self._cloudmap(db)
        with patch.object(CloudMap, "get_host", return_value=manager):
            return cloud_map.update_repository(url, **kw)

    def test_deleted_keeps_the_record(self):
        db = self._db_with_existing()
        record = self._update(db, self.SENT, status="deleted")
        assert record is not None and record.key == self.CANONICAL
        stored = db.get_repository(self.CANONICAL)
        assert stored and stored.status == "deleted"
        assert ("", "ensemble.yaml") in stored.contains
        # already deleted: nothing to change
        assert self._update(db, self.SENT, status="deleted") is None

    def test_private(self):
        db = self._db_with_existing()
        assert self._update(db, self.SENT, private=True) is not None
        stored = db.get_repository(self.CANONICAL)
        assert stored and stored.private is True and stored.status is None

    def test_moved_to_is_the_canonical_key(self):
        db = self._db_with_existing()
        record = self._update(
            db,
            self.SENT,
            status="moved",
            moved_to="http://gdk.test:3000/other/project.git",
        )
        assert record is not None
        assert (record.status, record.moved_to) == (
            "moved",
            "git://unfurl.cloud/other/project.git",
        )

    def test_missing_record_is_skipped(self):
        db = CloudMapDB("", contents={}, validate=False)
        assert self._update(db, self.SENT, status="deleted") is None
        assert db.get_repository(self.CANONICAL) is None


def test_post_cloudmap_merge_directive():
    from unfurl.server.cloudmap import _merge_deletes, _merge_record, _without_field

    assert _merge_deletes(True) == []
    assert _merge_deletes({"delete": ["moved_to", "/metadata/description"]}) == [
        ["moved_to"],
        ["metadata", "description"],
    ]
    for bad in ("yes", {"strategy": "replace"}, {"delete": "x"}, {"delete": ["a//b"]}):
        with pytest.raises(ValueError):
            _merge_deletes(bad)

    existing = {"path": "p", "moved_to": "git://x", "metadata": {"description": "d", "title": "t"}}
    merged = _merge_record(existing, {"status": "active"})
    for tokens in _merge_deletes({"delete": ["moved_to", "/metadata/description"]}):
        merged = _without_field(merged, tokens)
    assert merged == {"path": "p", "status": "active", "metadata": {"title": "t"}}
    # the existing record is left as it was
    assert existing["metadata"] == {"description": "d", "title": "t"}


def test_analyze_endpoint_clone_root():
    from unfurl.server.cloudmap import _analysis_clone_root
    from unfurl.server.serve import app

    configured = Mock()
    configured.get_context.return_value = {
        "cloudmaps": {
            "repositories": {
                "cloudmap": {"url": "https://example.com/cloudmap.git", "clone_root": "repos"}
            }
        }
    }
    unconfigured = Mock()
    unconfigured.get_context.return_value = {
        "cloudmaps": {"repositories": {"cloudmap": {"url": "https://example.com/cloudmap.git"}}}
    }
    with app.app_context(), patch.dict(app.config, UNFURL_CLONE_ROOT="/srv/clones"):
        assert _analysis_clone_root(configured) == "repos"
        assert _analysis_clone_root(unconfigured) == "/srv/clones"
        assert _analysis_clone_root(None) == "/srv/clones"


class TestAnalyzeCheckout:
    """``CloudMap.analyze_checkout`` analyzes a checkout as it is."""

    URL = "git://unfurl.cloud/group/project.git"

    def _checkout(self, tmp_path):
        repo = git.Repo.init(tmp_path / "project", initial_branch="main")
        repo.create_remote("origin", "https://unfurl.cloud/group/project.git")
        (tmp_path / "project" / "Dockerfile").write_text("FROM scratch\n")
        (tmp_path / "project" / ".gitlab-ci.yml").write_text("{}\n")
        repo.index.add(["Dockerfile", ".gitlab-ci.yml"])
        repo.index.commit("initial")
        return repo

    def _analyze(self, db, repo, branch, paths):
        from unfurl.repo import GitRepo

        cloud_map = CloudMap(None, "", db=db)
        return cloud_map.analyze_checkout(self.URL, GitRepo(repo), branch, paths)

    def _db(self):
        db = CloudMapDB("", contents={}, validate=False)
        db.add_record(Repository(url=self.URL, path="group/project", default_branch="main"))
        return db

    def test_new_file_analyzes_the_whole_repository(self, tmp_path):
        db = self._db()
        record = self._analyze(db, self._checkout(tmp_path), "main", ["Dockerfile"])
        assert record is not None
        assert db.get_artifact(f"{self.URL}#:Dockerfile")
        assert db.get_artifact(f"{self.URL}#:.gitlab-ci.yml")
        assert {key for _, key in record.contains} == {"Dockerfile", ".gitlab-ci.yml"}

    def test_known_files_are_reanalyzed_alone(self, tmp_path):
        db = self._db()
        repo = self._checkout(tmp_path)
        self._analyze(db, repo, "main", [])
        ci = db.get_artifact(f"{self.URL}#:.gitlab-ci.yml")
        assert ci
        db.delete_record(ci)
        record = self._analyze(db, repo, "main", ["Dockerfile"])
        assert db.get_artifact(f"{self.URL}#:Dockerfile")
        # the whole repository wasn't re-analyzed
        assert db.get_artifact(f"{self.URL}#:.gitlab-ci.yml") is None
        # and the other files' entries are left in contains
        assert record and ("", ".gitlab-ci.yml") in record.contains
        # a path that isn't an artifact yet: the whole repository is
        (tmp_path / "project" / "notes.txt").write_text("x")
        self._analyze(db, repo, "main", ["Dockerfile", "notes.txt"])
        assert db.get_artifact(f"{self.URL}#:.gitlab-ci.yml")

    def test_replace_collects_deleted_files(self, tmp_path):
        db = self._db()
        repo = self._checkout(tmp_path)
        self._analyze(db, repo, "main", [])
        assert db.get_artifact(f"{self.URL}#:.gitlab-ci.yml")
        repo.index.remove([".gitlab-ci.yml"], working_tree=True)
        (tmp_path / "project" / "notes.txt").write_text("x")
        repo.index.add(["notes.txt"])
        repo.index.commit("delete ci")
        self._analyze(db, repo, "main", ["notes.txt"])
        assert db.get_artifact(f"{self.URL}#:.gitlab-ci.yml") is None
        assert db.get_artifact(f"{self.URL}#:Dockerfile")

    def test_replace_keeps_to_its_branch(self, tmp_path):
        db = self._db()
        repo = self._checkout(tmp_path)
        self._analyze(db, repo, "main", [])
        repo.git.checkout("-b", "feature")
        self._analyze(db, repo, "feature", [])
        assert db.get_artifact(f"{self.URL}#feature:.gitlab-ci.yml")
        repo.index.remove([".gitlab-ci.yml"], working_tree=True)
        repo.index.commit("delete ci on the branch")
        self._analyze(db, repo, "feature", [])
        assert db.get_artifact(f"{self.URL}#feature:Dockerfile")
        assert db.get_artifact(f"{self.URL}#feature:.gitlab-ci.yml") is None
        # the default branch's artifacts aren't the branch's to collect
        assert db.get_artifact(f"{self.URL}#:.gitlab-ci.yml")
        repo.git.checkout("main")
        self._analyze(db, repo, "main", [])
        # nor the branch's the default branch's
        assert db.get_artifact(f"{self.URL}#feature:Dockerfile")

    def test_other_branches_are_qualified(self, tmp_path):
        db = self._db()
        repo = self._checkout(tmp_path)
        self._analyze(db, repo, "main", [])
        repo.git.checkout("-b", "feature")
        record = self._analyze(db, repo, "feature", ["Dockerfile"])
        assert record is not None
        assert db.get_artifact(f"{self.URL}#feature:Dockerfile")
        assert {key for _, key in record.contains} == {
            "Dockerfile",
            ".gitlab-ci.yml",
            "#feature:Dockerfile",
            "#feature:.gitlab-ci.yml",
        }
        assert record.revision == ""
        assert record.contains_artifact_url("#feature:Dockerfile") == (
            f"{self.URL}#feature:Dockerfile"
        )


def test_analyze_endpoint_repo_locator():
    from unfurl.server.cloudmap import _analysis_repo_locator
    from unfurl.server.serve import app

    config = dict(UNFURL_CLONE_ROOT="/srv/clones", UNFURL_CLOUD_SERVER="https://unfurl.cloud")
    with app.app_context(), patch.dict(app.config, config):
        locate = _analysis_repo_locator("/srv/other")
        # the server's own checkout of a project on the unfurl cloud server
        assert locate("git://unfurl.cloud/group/project.git", "main", False) == (
            "/srv/clones/public/group/project/main"
        )
        assert locate("git://unfurl.cloud/group/project.git", "trunk", True) == (
            "/srv/clones/private/group/project/trunk"
        )
        # anything else by host and path
        assert locate("git://github.com/owner/repo.git", "main", False) == (
            "/srv/other/github.com/owner/repo"
        )


def test_directory_repo_locator(tmp_path):
    """With a locator, a directory finds and clones repositories where the
    locator says rather than searching its root."""
    url = "git://example.com/group/project.git"
    located = tmp_path / "located"

    def locate(repo_url, branch, private):
        assert (repo_url, branch, private) == (url, "main", False)
        return str(located)

    upstream = git.Repo.init(tmp_path / "upstream", initial_branch="main")
    (tmp_path / "upstream" / "f").write_text("x")
    upstream.index.add(["f"])
    upstream.index.commit("initial")
    # a checkout under the root that only a search would find
    stray = git.Repo.clone_from(str(tmp_path / "upstream"), tmp_path / "root" / "stray")
    stray.remotes.origin.set_url("https://example.com/group/project.git")

    db = CloudMapDB("", contents={}, validate=False)
    cloud_map = CloudMap(
        None, "", localrepo_root=str(tmp_path / "root"), db=db, repo_locator=locate
    )
    directory = cloud_map.directory
    assert directory.find_repo(url, "") is None

    repo_info = Repository(url=url, path="group/project")
    cloned = directory.clone_repo(repo_info, str(tmp_path / "upstream"))
    assert os.path.samefile(cloned.working_dir, located)
    cloned.repo.remotes.origin.set_url("https://example.com/group/project.git")
    found = CloudMap(
        None, "", localrepo_root=str(tmp_path / "root"), db=db, repo_locator=locate
    ).directory.find_repo(url, "")
    assert found and os.path.samefile(found.working_dir, located)


def test_matches_source_by_branch():
    from unfurl.cloudmap.provenance import ProvenanceTrackingContext

    matches = ProvenanceTrackingContext.matches_source
    repo = "git://example.com/r.git"
    assert matches(f"{repo}#:f.yaml", repo)
    assert matches(f"{repo}#feature:f.yaml", f"{repo}#feature")
    # each branch collects only its own files
    assert not matches(f"{repo}#feature:f.yaml", repo)
    assert not matches(f"{repo}#:f.yaml", f"{repo}#feature")
    assert not matches(f"{repo}#feature2:f.yaml", f"{repo}#feature")
    # a file's url doesn't collect other paths
    assert not matches(f"{repo}#feature:f.yaml.bak", f"{repo}#feature:f.yaml")


def test_field_pointers_match_rust():
    """Shared with `field_pointers_match_python` in
    rust/git-sync/src/formats/cloudmap.rs."""
    import json
    from unfurl.cloudmap.provenance import field_pointers

    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "field_pointers.json").read_text()
    )
    assert sorted(field_pointers(fixture["record"])) == fixture["pointers"]


class TestAppliedOwnership:
    """Analysis only overwrites the fields it wrote last time
    (``metadata.discovery.applied``); any other write takes over the fields it
    changes."""

    REPO = "git://example.com/repo.git"
    KEY = "git://example.com/repo.git#:a.yaml"

    def _context(self):
        from unfurl.cloudmap.provenance import ProvenanceTrackingContext

        return ProvenanceTrackingContext(CloudMapDB("", contents={}, validate=False))

    def _analyze(self, context, source, **metadata):
        with context._tracking_provenance(source):
            context.add_record(
                Artifact(url=self.KEY, metadata=ArtifactMetadata(**metadata))
            )
        return context.get_artifact(self.KEY)

    def _applied(self, record, source):
        return record.metadata.discovery.applied.get(source, [])

    def test_a_manual_change_survives_reanalysis(self):
        context = self._context()
        record = self._analyze(context, self.REPO, title="T", description="analyzed")
        assert self._applied(record, self.REPO) == [
            "metadata/description",
            "metadata/title",
        ]
        record.metadata.description = "edited"
        context.add_record(record)
        assert self._applied(record, self.REPO) == ["metadata/title"]

        record = self._analyze(context, self.REPO, title="T2", description="reanalyzed")
        assert record.metadata.description == "edited"
        assert record.metadata.title == "T2"
        assert self._applied(record, self.REPO) == ["metadata/title"]

    def test_taking_over_every_field_isnt_forgotten(self):
        # a record left with no fields analysis owns isn't one analysis has
        # never tracked, which it would own entirely
        context = self._context()
        record = self._analyze(context, self.REPO, title="analyzed")
        record.metadata.title = "edited"
        context.add_record(record)
        assert record.metadata.discovery.applied == {self.REPO: []}
        record = self._analyze(context, self.REPO, title="reanalyzed")
        assert record.metadata.title == "edited"

    def test_a_manual_change_to_a_nested_key_is_kept(self):
        context = self._context()
        record = self._analyze_type(context, version="1.0", status="present")
        assert self._applied(record, self.REPO) == [
            "type/unfurl.nodes.App/status",
            "type/unfurl.nodes.App/version",
        ]
        record.type = TypeRefs(
            {"unfurl.nodes.App": TypeRefConstraint(version="1.0", status="failed")}
        )
        context.add_record(record)
        assert self._applied(record, self.REPO) == ["type/unfurl.nodes.App/version"]
        record = self._analyze_type(context, version="2.0", status="present")
        constraint = record.type.types["unfurl.nodes.App"]
        assert (constraint["version"], constraint["status"]) == ("2.0", "failed")

    def _analyze_type(self, context, **constraint):
        with context._tracking_provenance(self.REPO):
            context.add_record(
                Artifact(
                    url=self.KEY,
                    type=TypeRefs({"unfurl.nodes.App": TypeRefConstraint(**constraint)}),
                )
            )
        return context.get_artifact(self.KEY)

    def test_unapplied_keys_change_only_at_their_default(self):
        from unfurl.cloudmap.provenance import _default_leaves, _merge_owned

        defaults = _default_leaves(Artifact)
        source = self.REPO
        previous = {
            "metadata": {
                "title": "",  # the default, so analysis can set it
                "description": "someone's",
                "discovery": {"applied": {"https://api.example.com/a": []}},
            },
            # an entry of a map is never a default, even with no constraint
            "type": {"unfurl.nodes.App": None},
        }
        incoming = {
            "metadata": {"title": "T", "description": "analyzed"},
            "type": {"unfurl.nodes.App": {"version": "1.0"}},
        }
        merged, written = _merge_owned(previous, incoming, source, defaults, False)
        assert merged == {
            "metadata": {"title": "T", "description": "someone's"},
            "type": {"unfurl.nodes.App": None},
        }
        assert written == ["metadata/title"]

    def test_untracked_records(self):
        # a record written before `applied` was kept is its producer's...
        from unfurl.cloudmap.provenance import ProvenanceTrackingContext

        for source, sources, expected in (
            (self.REPO, [self.REPO], "reanalyzed"),
            # a record analyzed from its own url doesn't list it
            (self.KEY, [], "reanalyzed"),
            # ...but not a record another source, or a person, wrote
            (self.REPO, ["https://api.example.com/a"], "untracked"),
            (self.REPO, [], "untracked"),
        ):
            db = CloudMapDB("", contents={}, validate=False)
            db.add_record(
                Artifact(
                    url=self.KEY,
                    metadata=ArtifactMetadata(
                        title="untracked", discovery=Discovery(sources=sources)
                    ),
                )
            )
            record = self._analyze(
                ProvenanceTrackingContext(db), source, title="reanalyzed"
            )
            assert record.metadata.title == expected, (source, sources)

    def test_a_field_no_longer_produced_is_removed(self):
        context = self._context()
        self._analyze(context, self.REPO, title="T", description="analyzed")
        record = self._analyze(context, self.REPO, title="T")
        assert record.metadata.title == "T"
        assert not record.metadata.description

    def test_sources_keep_to_their_own_fields(self):
        context = self._context()
        other = "https://api.example.com/a"
        self._analyze(context, self.REPO, title="T")
        record = self._analyze(context, other, title="other", description="D")
        # the title was the repository's, so the other source leaves it
        assert (record.metadata.title, record.metadata.description) == ("T", "D")
        assert self._applied(record, other) == ["metadata/description"]
        record = self._analyze(context, self.REPO, title="T2")
        assert (record.metadata.title, record.metadata.description) == ("T2", "D")
        assert self._applied(record, self.REPO) == ["metadata/title"]

    def test_changes_made_in_place_are_seen(self):
        from unfurl.cloudmap.provenance import ProvenanceTrackingContext

        db = CloudMapDB("", contents={}, validate=False)
        other = "https://api.example.com/repo"
        db.add_record(
            Repository(
                url=self.REPO,
                path="repo",
                metadata=RepositoryMetadata(
                    discovery=Discovery(applied={other: ["path"]})
                ),
            )
        )
        context = ProvenanceTrackingContext(db)
        stored = context.get_repository(self.REPO)
        assert stored is not None
        # modified as read, before being added back
        stored.contains = TypeRefs.urls_fromdict({"a.yaml": None}, keys_are_urls=True)
        with context._tracking_provenance(self.REPO):
            context.add_record(stored)
        assert self._applied(stored, self.REPO) == ["contains/a.yaml"]
        assert self._applied(stored, other) == ["path"]

    def test_updating_the_stored_record_isnt_restricted(self):
        context = self._context()
        other = "https://api.example.com/a"
        self._analyze(context, other, title="other's", description="D")
        stored = self._analyze(context, self.REPO, title="T")
        assert stored.metadata.title == "other's"
        # retrieved and updated rather than produced anew
        stored.metadata.title = "T"
        stored.metadata.description = ""
        with context._tracking_provenance(self.REPO):
            context.add_record(stored)
        record = context.get_artifact(self.KEY)
        assert (record.metadata.title, record.metadata.description) == ("T", "")
        assert self._applied(record, self.REPO) == ["metadata/title"]
        assert self._applied(record, other) == []

    def test_updating_an_untracked_record_keeps_its_fields(self):
        # the untracked record was the source's, so the fields it leaves as
        # they were stay its own
        db = CloudMapDB("", contents={}, validate=False)
        db.add_record(
            Artifact(
                url=self.KEY,
                metadata=ArtifactMetadata(
                    title="T", description="D", discovery=Discovery(sources=[self.REPO])
                ),
            )
        )
        from unfurl.cloudmap.provenance import ProvenanceTrackingContext

        context = ProvenanceTrackingContext(db)
        stored = context.get_artifact(self.KEY)
        stored.metadata.title = "T2"
        with context._tracking_provenance(self.REPO):
            context.add_record(stored)
        assert self._applied(stored, self.REPO) == [
            "metadata/description",
            "metadata/title",
        ]


def test_schema_files_are_checked_once(monkeypatch, tmp_path):
    """A schema read from a file is checked against the metaschema the first
    time only; one without a file, every time."""
    from jsonschema import Draft7Validator
    from unfurl.util import find_schema_errors

    checked = []
    monkeypatch.setattr(Draft7Validator, "check_schema", checked.append)
    schema = {"type": "object"}
    path = str(tmp_path / "schema.json")
    for _ in range(2):
        assert find_schema_errors({}, schema, path) is None
    assert len(checked) == 1
    for _ in range(2):
        assert find_schema_errors({}, schema) is None
    assert len(checked) == 3
