import os
import traceback
from click.testing import CliRunner
import pytest
from unfurl.__main__ import cli
import git
from unfurl.tosca_plugins.cloudmap_defs import (
    Artifact,
    ArtifactMetadata,
    Discovery,
    EntitySchema,
    Instantiation,
    is_label,
    join_resource_url,
    Repository,
    RepositoryMetadata,
    TypeRefs,
    Service,
)
from unfurl.util import change_cwd, API_VERSION
from unfurl.repo import sanitize_url
from tests.utils import init_project, run_cmd, run_job_cmd
from unittest.mock import Mock, patch
from unfurl.cloudmap import (
    CloudMap,
    GitlabManager,
    GithubManager,
    CloudMapDB,
)
from unfurl.localenv import LocalEnv

UNFURL_TEST_CLOUDMAP_URL = os.getenv("UNFURL_TEST_CLOUDMAP_URL")

# Mark to skip integration tests if UNFURL_TEST_CLOUDMAP_URL not set
skip_integration = pytest.mark.skipif(
    not UNFURL_TEST_CLOUDMAP_URL,
    reason="need UNFURL_TEST_CLOUDMAP_URL set to run integration test",
)


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
        url: file:../cloudmap
    cloudmaps:
      repositories:
        cloudmap:
          # url: file:../cloudmap # configurator needs it to be a regular repository
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

expected_cloudmap = """\
apiVersion: unfurl/v1.0.0
kind: CloudMap
repositories:
  git://unfurl.cloud/feb20a/dashboard.git:
    path: feb20a/dashboard
    name: dashboard
    protocols:
    - https
    - ssh
    project_url: https://unfurl.cloud/feb20a/dashboard
    metadata:
      homepage_url: https://unfurl.cloud/feb20a/dashboard
      issues_url: https://unfurl.cloud/feb20a/dashboard/-/issues
    private: true
    default_branch: main
    branches:
      main: 4551885dfab39991cfdb958cb79fcb6aa282481d
    contains:
      .gitlab-ci.yml:
        cloudmap.artifacts.GitLabPipeline:
      ensemble/ensemble.yaml:
        cloudmap.artifacts.unfurl.Ensemble:
      environments/aws/onecommons/blueprints/odoo/odoo-aws-1/ensemble.yaml:
        cloudmap.artifacts.unfurl.Ensemble:
  git://unfurl.cloud/onecommons/blueprints/odoo.git:
    path: onecommons/blueprints/odoo
    name: Odoo
    protocols:
    - https
    - ssh
    project_url: https://unfurl.cloud/onecommons/blueprints/odoo
    metadata:
      description: Odoo is a suite of business management software tools including
        CRM, e-commerce, billing, accounting, manufacturing, warehouse, project management,
        and inventory management. The Community version is a libre software, licensed
        under the GNU LGPLv3.
      homepage_url: https://unfurl.cloud/onecommons/blueprints/odoo
      issues_url: https://unfurl.cloud/onecommons/blueprints/odoo/-/issues
    default_branch: main
    branches:
      main: ce8f368b18f111c3afec3d5e3ceb40a2bb02095d
    tags:
      v1.0.0: 2e57b3251bd9f8e292385b9f31774f6408abc4d7
      v0.1.0: be0da659d7358bc4d24d988f436a5508e098b252
    contains:
      .gitlab-ci.yml:
        cloudmap.artifacts.GitLabPipeline:
      ensemble-template.yaml#spec/service_template:
        cloudmap.artifacts.tosca.ServiceTemplate:
      unfurl.yaml:
        cloudmap.artifacts.unfurl.Project:
  git://unfurl.cloud/onecommons/std.git:
    path: onecommons/std
    name: std
    protocols:
    - https
    - ssh
    project_url: https://unfurl.cloud/onecommons/std
    metadata:
      topics:
      - documentation
      - library
      homepage_url: https://unfurl.cloud/onecommons/std
      issues_url: https://unfurl.cloud/onecommons/std/-/issues
    default_branch: main
    branches:
      main: f7e321a77adcc06eff3c317833bcc7009d2d3bd3
      stable: 05d48dc2257da2f5d94351671c14d32385bd19e4
    tags:
      v1.1.1: 05d48dc2257da2f5d94351671c14d32385bd19e4
      v1.1.0: 62ce5b304f12c1a810930d849f17b460cd2999f0
      v1.0.4: 0882290e21430f852deb8ebfd1f3b418f7a2ce5d
      v1.0.3: 287356887dd1a44a9fa9b5ace8992259bb0887bf
      v1.0.2: 14c1b3bfff0670731a329d75c5b05ac5cb8daca7
      v1.0.1: 254e5a87af19aa7e39cab8a02f6b27df8fcfe348
      v1.0.0: 8ed0136c658cf22f1aab3242a536470d3c35f28e
      INITIAL: 0e306c3e627b9c027b0abb6d3969b01126c2ffe1
    contains:
      .devcontainer/Containerfile:
        cloudmap.artifacts.Containerfile:
      .gitlab-ci.yml:
        cloudmap.artifacts.GitLabPipeline:
      dummy-ensemble.yaml:
        cloudmap.artifacts.tosca.TypeLibrary:
  git://unfurl.cloud/onecommons/unfurl-types.git:
    path: onecommons/unfurl-types
    name: unfurl-types
    protocols:
    - https
    - ssh
    project_url: https://unfurl.cloud/onecommons/unfurl-types
    metadata:
      homepage_url: https://unfurl.cloud/onecommons/unfurl-types
      issues_url: https://unfurl.cloud/onecommons/unfurl-types/-/issues
    default_branch: main
    branches:
      abreidenbach/cname-dns-type: b25da294cc2168d4c6be6702c60aae0091c12e11
      abreidenbach/fix-incremental-deploy: addb09e9060bbedc9b50eddc9f337e46900b8166
      jg-changes: 67dd87eaa09d8f7001b9fbce7221485478b2d470
      main: 0fff800942311706ff45cbb9b07c0a9ec8121c22
      replace-access-token: a372bc876aed8d52332ca0fb22727c3231f97170
      transitional: 589ca2fe42f9177a0a18081a8ebea3f6b7bef905
    tags:
      v0.7.7: 0fff800942311706ff45cbb9b07c0a9ec8121c22
      v0.7.6: 2bbeba877bbd28ac0932275ce1254fca374f63d1
      v0.7.5: 3dd6c1b010eb1a6e538851b482bba5ce0998140c
      v0.7.4: 328513a4efebe060b80f654144e6503bd68bdfca
      v0.7.3: 1a7d90a7797e84b4bc19750d640654b926d37916
      v0.7.2: dcc4f8cf1c9a0908e7b37e3837db3a6994dd3bbe
      v0.7.1: 3cdda3d789dce4520a87dead1eb8c812ac00e479
      v0.7.0: 700993d437992a10e39ecad6a7669aa84d0779d0
      v0.6.5: e1cff702297589d6130007c11a82e62920f42393
      v0.6.4: c90b4fb3333a64aae1dd458534e61a9f451e4196
      v0.6.3: 995a6227eaa8392b9b582f1a2a10e6c1cdc8cdc9
      v0.6.2: 7924982c690a00117cff421b7afd162a950564cd
      v0.6.1: 19910bc480d0c45327afd63fba79c6755ce25270
      v0.6.0: 5687d4c9e2f91b0b256ec36ed4b8867b93442974
      v0.5.1: f5dd4685b4e7aba04fda158ceb1de5df943132b1
      v0.5.0: 75ac354f2ebe8a2aa3ca06b057cfc0a6a49c80c5
      v0.4.1: 1746e6e4cc08ee64ed477c6c0d2968365c1751ba
      v0.4.0: d8fd4bc3b38e113e9fbfb9495b2e862083931923
      v0.3.3: 068eb8dc96f879d670336e97ba2551da9eaba2f5
      v0.3.2: 365441c5f52527c6acfb6eef80470ea1019c6f6a
      v0.3.1: 1598b16844c814b7ebdcadef936573816cb73132
      v0.3.0: c27a4b9f2730766b09768cd8dfadd9dbf0c76c82
      v0.2.0: 3368772296efe28b409f13d793e066b2f5f5f1a6
      v0.1.0: d0e8f921478f864da2037e1143afe3dd35e590ad
      before-caddy: aa831e8cfc58c84e5ecfe963afe6c472b1cb9476
    contains:
      .gitlab-ci.yml:
        cloudmap.artifacts.GitLabPipeline:
      dummy-ensemble.yaml:
        cloudmap.artifacts.tosca.TypeLibrary:
artifacts:
  git://unfurl.cloud/feb20a/dashboard.git#:.gitlab-ci.yml:
    type:
      cloudmap.artifacts.GitLabPipeline:
  git://unfurl.cloud/feb20a/dashboard.git#:ensemble/ensemble.yaml:
    type:
      cloudmap.artifacts.unfurl.Ensemble:
    references:
      git://unfurl.cloud/onecommons/std.git#v1.1.1:.:
        cloudmap.artifacts.unfurl.Package:
          version: 1.1.1
    digest: git:tree:5fe07694589fe54e2fb60f250e793db684bbeb95
  git://unfurl.cloud/feb20a/dashboard.git#:environments/aws/onecommons/blueprints/odoo/odoo-aws-1/ensemble.yaml:
    type:
      cloudmap.artifacts.unfurl.Ensemble:
        version: 0.1
    references:
      git://unfurl.cloud/onecommons/std.git#v1.1.1:.:
        cloudmap.artifacts.unfurl.Package:
          version: 1.1.1
      pkg:oci/odoo?repository_url=docker.io/bitnami/odoo&tag=latest:
    instantiates:
      the_app:
        Odoo@unfurl.cloud/onecommons/blueprints/odoo:
    dependencies:
      aws:
        unfurl.relationships.ConnectsTo.AWSAccount:
      gcp:
        unfurl.relationships.ConnectsTo.GoogleCloudProject:
      odoo-aws-1:
        unfurl.relationships.ConnectsTo.AWSAccount:
    digest: git:blob:8e784df418a595b84a916be749024ec967ef1a60
    metadata:
      title: Odoo
      version: 0.1
  git://unfurl.cloud/onecommons/blueprints/odoo.git#:.gitlab-ci.yml:
    type:
      cloudmap.artifacts.GitLabPipeline:
  git://unfurl.cloud/onecommons/blueprints/odoo.git#:ensemble-template.yaml%23spec/service_template:
    type:
      cloudmap.artifacts.tosca.ServiceTemplate:
        version: 0.1
    references:
      git://unfurl.cloud/onecommons/unfurl-types#v0.7.7:.:
        cloudmap.artifacts.unfurl.Package:
          version: 0.7.7
      pkg:oci/odoo?repository_url=docker.io/bitnami/odoo&tag=latest:
    instantiates:
      the_app:
        Odoo@unfurl.cloud/onecommons/blueprints/odoo:
    dependencies:
      Database:
        PostgresDB@unfurl.cloud/onecommons/unfurl-types:
      aws:
        unfurl.relationships.ConnectsTo.AWSAccount:
      gcp:
        unfurl.relationships.ConnectsTo.GoogleCloudProject:
    digest: git:blob:79a33678461da4788790a2d005402a16481b0a85
    metadata:
      title: Odoo
      version: 0.1
  git://unfurl.cloud/onecommons/std.git#:.devcontainer/Containerfile:
    type:
      cloudmap.artifacts.Containerfile:
  git://unfurl.cloud/onecommons/std.git#:.gitlab-ci.yml:
    type:
      cloudmap.artifacts.GitLabPipeline:
  git://unfurl.cloud/onecommons/std.git#:dummy-ensemble.yaml:
    type:
      cloudmap.artifacts.tosca.TypeLibrary:
    digest: git:blob:38db686f4aa92b52bafa9cb484f6ee976c33029a
  git://unfurl.cloud/onecommons/unfurl-types.git#:.gitlab-ci.yml:
    type:
      cloudmap.artifacts.GitLabPipeline:
  git://unfurl.cloud/onecommons/unfurl-types.git#:dummy-ensemble.yaml:
    type:
      cloudmap.artifacts.tosca.TypeLibrary:
    digest: git:blob:ff067b4f5c851a1aacca2c1db96ae04ef30c881d
  pkg:oci/odoo?repository_url=docker.io/bitnami/odoo&tag=latest:
    type:
      cloudmap.artifacts.oci.Image:
    metadata:
      description: Bitnami Secure Image for odoo
    discovery:
      sources:
      - https://hub.docker.com/v2/repositories/bitnami/odoo/
services:
  https://example.com/oodo:
    type:
      Odoo@unfurl.cloud/onecommons/blueprints/odoo:
    instantiated_by:
      git://unfurl.cloud/feb20a/dashboard.git#:environments/aws/onecommons/blueprints/odoo/odoo-aws-1/ensemble.yaml:
instantiations:
  git://unfurl.cloud/feb20a/dashboard.git#:environments/aws/onecommons/blueprints/odoo/odoo-aws-1/ensemble.yaml:
    type:
      cloudmap.artifacts.unfurl.Ensemble:
    revision: 4551885dfab39991cfdb958cb79fcb6aa282481d
    source: git://unfurl.cloud/onecommons/blueprints/odoo.git#:ensemble-template.yaml%23spec/service_template
    source_revision: 2e57b3251bd9f8e292385b9f31774f6408abc4d7
    instantiated:
      https://example.com/oodo:
    inputs:
      git://unfurl.cloud/onecommons/std.git#v1.1.1:.:
        cloudmap.artifacts.unfurl.Package:
          version: 1.1.1
      pkg:oci/odoo?repository_url=docker.io/bitnami/odoo&tag=latest:
types:
  Odoo@unfurl.cloud/onecommons/blueprints/odoo:
    name: Odoo@unfurl.cloud/onecommons/blueprints/odoo
    kind: Component
    extends:
    - Odoo@unfurl.cloud/onecommons/blueprints/odoo
    - unfurl.nodes.SoftwareService@unfurl.cloud/onecommons/std:generic_types
    - SoftwareService@unfurl.cloud/onecommons/std:generic_types
    - SoftwareComponent@unfurl.cloud/onecommons/std:generic_types
    - tosca.nodes.Root
    - tosca.capabilities.Node
    - tosca.capabilities.Root
    metadata:
      title: Odoo
  unfurl.relationships.ConnectsTo.AWSAccount:
    name: unfurl.relationships.ConnectsTo.AWSAccount
    kind: Component
    extends:
    - unfurl.relationships.ConnectsTo.AWSAccount
    - unfurl.relationships.ConnectsTo.CloudAccount
    - unfurl.relationships.ConnectsTo.ComputeMachines
    - tosca.relationships.ConnectsTo
    - tosca.relationships.Root
    - unfurl.relationships.ConnectsTo.ObjectStorage
    metadata:
      title: AWSAccount
  unfurl.relationships.ConnectsTo.GoogleCloudProject:
    name: unfurl.relationships.ConnectsTo.GoogleCloudProject
    kind: Component
    extends:
    - unfurl.relationships.ConnectsTo.GoogleCloudProject
    - unfurl.relationships.ConnectsTo.CloudAccount
    - unfurl.relationships.ConnectsTo.ComputeMachines
    - tosca.relationships.ConnectsTo
    - tosca.relationships.Root
    metadata:
      title: GoogleCloudProject
"""

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
            # print("cloudmap\n", cloudmap)
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
    kind: Component
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


@pytest.mark.skipif(
    GithubManager is None, reason="PyGithub not installed"
)
class TestGithubManager:
    """Unit tests for GithubManager using mock GitHub API objects."""

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
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

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
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

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
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

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
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

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
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

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
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

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
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

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
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

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
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

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
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

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
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

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
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

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
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

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
    def test_canonize_without_canonical_url(self, mock_auth, mock_github_class):
        """Test URL canonization without canonical URL."""
        config = {"type": "github", "url": "https://github.com", "password": "token"}
        manager = GithubManager("test", config)

        url = "https://github.com/user/repo.git"
        result = manager.canonize(url)

        assert result == url

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
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

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
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

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
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

    @patch("unfurl.cloudmap.gitlab.Gitlab")
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

    @patch("unfurl.cloudmap.gitlab.Gitlab")
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

    @patch("unfurl.cloudmap.gitlab.Gitlab")
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

    @patch("unfurl.cloudmap.gitlab.Gitlab")
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
    assert join_resource_url("git://a.com/x.git", "{=var}") == "git://a.com/x.git#{=var}"
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
    policies:
      spdx_licenses: MIT
      terms_of_service: https://unfurl.cloud/terms
      privacy_policy: https://unfurl.cloud/privacy
    instantiated_by:
      "#/instantiations/2023-09-24T15:31:00Z":
    discovery:
      last_checked: "2023-09-24T15:30:00Z"
      sources:
      - https://unfurl.cloud/api/v1/metadata
types:
  Zulip@unfurl.cloud/onecommons/blueprints/zulip:
    kind: Component
    source: git://unfurl.cloud/onecommons/blueprints/zulip.git#:types/app.yaml
    metadata:
      title: Zulip
    extends:
    - unfurl.nodes.WebApp@unfurl.cloud/onecommons/std:generic_types
    - WebApp@unfurl.cloud/onecommons/std:generic_types
  software.Nginx@unfurl.cloud/onecommons/std:
    kind: Component
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
        assert artifact.discovery.last_checked == "2023-09-24T15:30:00Z"
        assert len(artifact.discovery.sources) == 2

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
        assert service.policies.spdx_licenses == "MIT"
        assert len(service.instantiated_by) == 1
        assert service.discovery.last_checked == "2023-09-24T15:30:00Z"
        assert len(service.discovery.sources) == 1

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
        assert zulip_type.kind == "Component"
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
        assert nginx_type.kind == "Component"
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
    assert isinstance(body, list)
    assert len(body) == 2, "response is a [primary, followed] pair"
    primary, followed = body
    assert "repositories" in primary
    assert "artifacts" in primary
    assert followed == {}


def test_cloudmap_endpoint_kind_only(cloudmap_test_client):
    resp = cloudmap_test_client.get("/cloudmap?kind=repositories")
    assert resp.status_code == 200
    primary, followed = resp.get_json()
    assert "repositories" in primary
    assert "artifacts" not in primary
    assert followed == {}


def test_cloudmap_endpoint_kind_and_key(cloudmap_test_client):
    from urllib.parse import quote

    key = "git://unfurl.cloud/onecommons/blueprints/odoo.git"
    resp = cloudmap_test_client.get(f"/cloudmap?kind=repositories&key={quote(key)}")
    assert resp.status_code == 200
    primary, followed = resp.get_json()
    assert list(primary["repositories"].keys()) == [key]
    assert followed == {}


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
    _primary, followed = resp.get_json()
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
    _primary, followed = resp.get_json()
    total = sum(len(records) for records in followed.values())
    assert total == 2


def test_cloudmap_endpoint_follow_zero(cloudmap_test_client):
    from urllib.parse import quote

    key = "git://unfurl.cloud/onecommons/blueprints/odoo.git"
    resp = cloudmap_test_client.get(
        f"/cloudmap?kind=repositories&key={quote(key)}&follow=0"
    )
    assert resp.status_code == 200
    _primary, followed = resp.get_json()
    assert followed == {}


def test_cloudmap_endpoint_follow_without_key(cloudmap_test_client):
    resp = cloudmap_test_client.get("/cloudmap?follow=10")
    assert resp.status_code == 200
    _primary, followed = resp.get_json()
    assert followed == {}


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


def _capture_graph(db: CloudMapDB, start_url: str = "") -> str:
    """Capture cloudmap_graph_console output as plain text (no color/markup)."""
    from unfurl.reporting import cloudmap_graph_console
    from rich.console import Console
    from io import StringIO

    buf = StringIO()
    console = Console(file=buf, force_terminal=False, no_color=True, width=200)
    cloudmap_graph_console(db, start_url, console=console)
    return buf.getvalue()


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
│   │           │       │           │   │        Odoo (cloudmap.artifacts.tosca.ServiceTemplate) [seen]
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
│   │           │                   │   │        Odoo (cloudmap.artifacts.tosca.ServiceTemplate) [seen]
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
│           │   │        Odoo (cloudmap.artifacts.tosca.ServiceTemplate) [seen]
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
            │   │        Odoo (cloudmap.artifacts.tosca.ServiceTemplate) [seen]
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
│       │           │   │        Odoo (cloudmap.artifacts.tosca.ServiceTemplate) [seen]
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
│                   │   │        Odoo (cloudmap.artifacts.tosca.ServiceTemplate) [seen]
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
    expected_dual = json.loads(
        (fixture_dir / "cloudmap_graph_dual.json").read_text()
    )
    assert dual_result == expected_dual

    # Not found
    assert cloudmap_graph_json(db, "nonexistent://url") == {
        "error": "Record not found: nonexistent://url",
    }

    # Full graph: compare against saved fixture
    full = cloudmap_graph_json(db)
    expected_full = json.loads((fixture_dir / "cloudmap_graph.json").read_text())
    assert full == expected_full


if __name__ == "__main__":
    """Update this file in-place.

    Usage:
        python tests/test_cloudmap.py                  # regenerate expected graph outputs
        python tests/test_cloudmap.py cloudmap.yaml    # replace expected_cloudmap with file contents

    To rebuild "expected_cloudmap" run test_create[] with $UNFURL_TEST_TMPDIR set, the cloudmap.yaml path will look like:
    $UNFURL_TEST_TMPDIR/tmpkjk37eir/project/cloudmap/cloudmap.yaml
    """
    import json
    import re
    import sys
    from pathlib import Path

    src = Path(__file__).read_text()

    if len(sys.argv) > 1:
        # Replace expected_cloudmap with contents of the given file
        cloudmap_file = Path(sys.argv[1])
        contents = cloudmap_file.read_text()
        pattern = r'(expected_cloudmap = )""".*?"""'
        replacement = f'expected_cloudmap = """\\\n{contents}"""'
        src, count = re.subn(pattern, replacement, src, count=1, flags=re.DOTALL)
        if count:
            print(f"expected_cloudmap: updated from {cloudmap_file}")
        else:
            print("expected_cloudmap: NOT FOUND in source")
        Path(__file__).write_text(src)
        fixture_dir = Path(__file__).parent / "fixtures"
        fixture_dir.mkdir(exist_ok=True)
        (fixture_dir / "expected_cloudmap.yaml").write_text(contents)
        print(
            f"expected_cloudmap.yaml: updated ({fixture_dir / 'expected_cloudmap.yaml'})"
        )
        print(f"Updated {Path(__file__).name}")
    else:
        # Regenerate expected graph outputs from expected_cloudmap
        fixture_dir = Path(__file__).parent / "fixtures"
        fixture_dir.mkdir(exist_ok=True)
        cloudmap_fixture = fixture_dir / "expected_cloudmap.yaml"
        cloudmap_fixture.write_text(expected_cloudmap)
        print(f"expected_cloudmap.yaml: updated ({cloudmap_fixture})")

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
