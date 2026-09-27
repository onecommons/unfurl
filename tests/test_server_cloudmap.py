import json
import os
import pytest
import requests
from click.testing import CliRunner
from git import Repo
from multiprocessing import get_context
from unfurl.repo import GitRepo
from unfurl.server import cloudmap as server_cloudmap, serve as server
from tests.server_utils import (
    RERUN_ON_SERVER_ERROR,
    HOST,
    _canonical,
    _cloudmap_db_url,
    _env_for,
    _next_port,
    _terminate_process,
    runner,
    serve_server,
    server_env,
    start_server_process,
)

pytestmark = RERUN_ON_SERVER_ERROR


@pytest.mark.parametrize("server_env", server_env)
def test_server_cloudmap(server_env):
    """Test /cloudmap and /graph endpoints across all server variants.

    Variants exercised:

    - ``no-redis`` / ``redis``: pure Python; /cloudmap and /graph read
      ``cloudmap.yaml`` directly.
    - ``redis-rust``: rust proxy with the cloudmap fast-path
      (``UNFURL_CLOUDMAP_REPO`` + ``UNFURL_CLOUDMAP_DB_URL`` set).
      /graph reaches the fast-path through ``CloudMapProxy`` (wired up
      via ``UNFURL_LOCAL_CLOUDMAP_URL``); /cloudmap POSTs stage in-flight
      (``commit=None``) instead of committing synchronously.
    - ``queue-rust``: rust proxy *without* a cloudmap-sync DB. /cloudmap
      and /graph are proxied through to Python — same behavior as the
      pure-Python variants. This is the rust passthrough path.
    """
    from pathlib import Path

    is_rust = "rust" in server_env
    # Only the ``redis-rust`` variant configures the rust cloudmap
    # fast-path; ``queue-rust`` runs the rust proxy *without* a
    # cloudmap-sync DB, so /cloudmap and /graph are proxied through to
    # the Python backend (the YAML path) — that's the variant we use to
    # exercise the passthrough.
    rust_cloudmap_local = server_env == "redis-rust"

    fixture_dir = Path(__file__).parent / "fixtures"
    cloudmap_content = (fixture_dir / "expected_cloudmap.yaml").read_text()

    runner = CliRunner()
    port = _next_port()
    with runner.isolated_filesystem() as tmpdir:
        # Create a git repo with cloudmap.yaml at CWD
        with open("cloudmap.yaml", "w") as f:
            f.write(cloudmap_content)
        # A second cloudmap document for the `cloudmap_path` assertions. It
        # must exist before the server starts because the rust fast-path
        # indexes the worktree once, at startup. The name sorts after
        # cloudmap.yaml so the worktree's auto-picked `default_file_path`
        # (MIN(file_path)) still points at the primary cloudmap.
        alt_path = "zz-alt-cloudmap.yaml"
        alt_key = "git://example.com/only-in-alt.git"
        with open(alt_path, "w") as f:
            f.write(
                "apiVersion: unfurl/v1.0.0\nkind: CloudMap\n"
                "repositories:\n"
                f"  {alt_key}:\n"
                f"    git: {alt_key}\n"
                "    path: only/in-alt\n"
                "    name: only-in-alt\n"
            )
        repo = GitRepo(Repo.init("."))
        repo.add_all(os.path.abspath("."))
        repo.commit_files(
            [os.path.abspath("cloudmap.yaml"), os.path.abspath(alt_path)],
            "Add cloudmap",
        )

        extra_env = _env_for(server_env, "server-cloudmap")
        if rust_cloudmap_local:
            extra_env["UNFURL_CLOUDMAP_REPO"] = os.path.abspath(".")
            extra_env["UNFURL_CLOUDMAP_DB_URL"] = _cloudmap_db_url()

        ctx = get_context("spawn")
        error_queue = ctx.Queue()
        p = ctx.Process(
            target=serve_server,
            args=(HOST, port, None, ".", f"{tmpdir}", {"home": ""}),
            kwargs={"error_queue": error_queue, "extra_env": extra_env},
        )
        p._error_queue = error_queue
        try:
            start_server_process(p, port, is_rust=is_rust)
            base = f"http://{HOST}:{port}/graph"

            # Full graph. Scoped to cloudmap.yaml so the second cloudmap in
            # the worktree stays out of it: a request naming no cloudmap_path
            # means "the default file" to the Python handler but "every
            # indexed file" to the rust one (see the rust integration test
            # `get_without_cloudmap_path_spans_every_file`).
            res = requests.get(base, params={"cloudmap_path": "cloudmap.yaml"})
            assert res.status_code == 200
            expected_full = json.loads(
                (fixture_dir / "cloudmap_graph.json").read_text()
            )
            # Compared through `_canonical` because a postgres-backed rust
            # fast-path returns the record maps in jsonb's normalised key
            # order, which reorders the `rels` lists built from them.
            assert _canonical(res.json()) == _canonical(expected_full)

            # Single artifact query
            artifact_url = "git://unfurl.cloud/onecommons/blueprints/odoo.git#:ensemble-template.yaml%23spec/service_template"
            res = requests.get(base, params={"url": artifact_url})
            assert res.status_code == 200
            expected_artifact = json.loads(
                (fixture_dir / "cloudmap_graph_artifact.json").read_text()
            )
            assert _canonical(res.json()) == _canonical(expected_artifact)

            # Dual record query (URL in both artifacts and instantiations)
            dual_url = "git://unfurl.cloud/feb20a/dashboard.git#:environments/aws/onecommons/blueprints/odoo/odoo-aws-1/ensemble.yaml"
            res = requests.get(base, params={"url": dual_url})
            assert res.status_code == 200
            expected_dual = json.loads(
                (fixture_dir / "cloudmap_graph_dual.json").read_text()
            )
            assert _canonical(res.json()) == _canonical(expected_dual)

            # Not found
            res = requests.get(base, params={"url": "nonexistent://url"})
            assert res.status_code == 404

            # ----- GET /cloudmap?type=... (rust fast-path and Python
            # YAML fallback must agree) -----
            cloudmap_url = f"http://{HOST}:{port}/cloudmap"

            # Exact declared-type match: only the four .gitlab-ci.yml
            # artifacts declare the pipeline type, so the filtered doc
            # contains just the artifacts section.
            res = requests.get(
                cloudmap_url, params={"type": "cloudmap.artifacts.GitLabPipeline"}
            )
            assert res.status_code == 200, res.text
            body = res.json()
            primary = body["result"]
            assert list(primary) == ["artifacts"]
            assert len(primary["artifacts"]) == 4
            assert all(k.endswith(".gitlab-ci.yml") for k in primary["artifacts"])
            assert "followed" not in body

            # Subtype match via `extends`: the service declares type
            # `Odoo@…`, whose type record (transitively) extends
            # `SoftwareService@…` — querying the base type matches it.
            res = requests.get(
                cloudmap_url,
                params={
                    "type": "SoftwareService@unfurl.cloud/onecommons/std:generic_types"
                },
            )
            assert res.status_code == 200, res.text
            primary = res.json()["result"]
            assert list(primary) == ["services"]
            assert list(primary["services"]) == ["https://example.com/oodo"]

            # No record declares the type (or a subtype) → empty doc,
            # not an error.
            res = requests.get(
                cloudmap_url, params={"type": "tosca.relationships.ConnectsTo"}
            )
            assert res.status_code == 200, res.text
            assert res.json() == {"result": {}}

            # kind + key + type AND together: matching type (via
            # extends: Odoo@… extends tosca.nodes.Root) → 200 ...
            service_key = "https://example.com/oodo"
            res = requests.get(
                cloudmap_url,
                params={
                    "kind": "services",
                    "key": service_key,
                    "type": "tosca.nodes.Root",
                },
            )
            assert res.status_code == 200, res.text
            assert service_key in res.json()["result"]["services"]

            # ... and a record whose type doesn't satisfy the filter
            # → 404.
            res = requests.get(
                cloudmap_url,
                params={
                    "kind": "services",
                    "key": service_key,
                    "type": "cloudmap.artifacts.oci.Image",
                },
            )
            assert res.status_code == 404, res.text

            # ----- GET /cloudmap?follow=... without a key (both servers
            # walk outward from every record the query selected) -----

            res = requests.get(
                cloudmap_url, params={"kind": "repositories", "follow": 100}
            )
            assert res.status_code == 200, res.text
            body = res.json()
            roots = set(body["result"]["repositories"])
            assert roots, "fixture should have repositories to walk from"
            followed = body["followed"]
            assert followed, "repositories reference records in other sections"
            assert set(followed) - {"repositories"}, (
                f"the walk should leave the starting section: {list(followed)}"
            )
            # roots are already in `result`; they are not repeated
            assert not (set(followed.get("repositories", {})) & roots)

            # the cap counts records, not roots
            res = requests.get(
                cloudmap_url, params={"kind": "repositories", "follow": 3}
            )
            assert res.status_code == 200, res.text
            capped = res.json()["followed"]
            assert sum(len(v) for v in capped.values()) == 3, capped

            # a paged walk starts from that page's records, capped per page
            res = requests.get(
                cloudmap_url,
                params={"kind": "repositories", "limit": 2, "follow": 10},
            )
            assert res.status_code == 200, res.text
            paged = res.json()
            assert len(paged["result"]["repositories"]) == 2
            assert paged["followed"], "the page references other records"
            assert sum(len(v) for v in paged["followed"].values()) <= 10

            # a one-record page walks exactly what that record reaches --
            # not what the limit+1 probe record reaches
            res = requests.get(
                cloudmap_url,
                params={"kind": "repositories", "limit": 1, "follow": 100},
            )
            assert res.status_code == 200, res.text
            one = res.json()
            only_key = next(iter(one["result"]["repositories"]))
            res = requests.get(
                cloudmap_url,
                params={"kind": "repositories", "key": only_key, "follow": 100},
            )
            assert res.status_code == 200, res.text
            assert _canonical(one["followed"]) == _canonical(res.json()["followed"])

            # ----- GET /cloudmap?limit=... (paging; the rust fast-path
            # cuts the page in SQL, the Python fallback slices the loaded
            # document — the walks must agree, and a page token minted by
            # one has to be understood by the other) -----

            res = requests.get(cloudmap_url, params={"kind": "artifacts"})
            assert res.status_code == 200, res.text
            unpaged = sorted(res.json()["result"]["artifacts"])
            assert len(unpaged) > 2, "fixture should be worth paging"

            seen: list = []
            token = None
            for _ in range(50):
                params = {"kind": "artifacts", "limit": 2}
                if token:
                    params["page_token"] = token
                res = requests.get(cloudmap_url, params=params)
                assert res.status_code == 200, res.text
                body = res.json()
                assert "followed" not in body, "a paged request is keyless"
                seen.extend(body["result"].get("artifacts", {}))
                token = body.get("next_page_token")
                if not token:
                    break
            else:
                raise AssertionError("paging failed to terminate")
            assert seen == sorted(seen), "records come back in key order"
            assert seen == unpaged, "pages concatenate to the unpaged section"

            # A limit at or above the section size is one page, no token.
            res = requests.get(
                cloudmap_url, params={"kind": "artifacts", "limit": len(unpaged)}
            )
            assert res.status_code == 200, res.text
            assert "next_page_token" not in res.json()

            # `type` narrows before the page is cut.
            res = requests.get(
                cloudmap_url,
                params={
                    "kind": "artifacts",
                    "type": "cloudmap.artifacts.GitLabPipeline",
                    "limit": 50,
                },
            )
            assert res.status_code == 200, res.text
            assert len(res.json()["result"]["artifacts"]) == 4

            # Error parity: a key can't be paged, a limit below 1 violates
            # its schema bound, and a malformed cursor is a bad request.
            res = requests.get(
                cloudmap_url,
                params={"kind": "services", "key": service_key, "limit": 2},
            )
            assert res.status_code == 400, res.text
            res = requests.get(cloudmap_url, params={"kind": "artifacts", "limit": 0})
            assert res.status_code == 422, res.text
            res = requests.get(
                cloudmap_url,
                params={"kind": "artifacts", "limit": 2, "page_token": "nosuchsection/key"},
            )
            assert res.status_code == 400, res.text

            # ----- GET /cloudmap?filter=... (content filter; the rust
            # fast-path pushes it into SQL, the Python fallback runs the
            # same predicate over the loaded document — they must agree) -----

            # Each value form gets a positive and a negative case. The
            # expected records come from the fixture, which happens to carry
            # every JSON type: a boolean (`private`), a float
            # (`metadata/version`), nulls (typeRef values), arrays
            # (`discovery/sources`) and objects (`branches`).
            def cloudmap_filter(expr):
                res = requests.get(cloudmap_url, params={"filter": expr})
                assert res.status_code == 200, res.text
                body = res.json()
                primary = body["result"]
                assert "followed" not in body, primary
                return primary

            dashboard = "git://unfurl.cloud/feb20a/dashboard.git"

            # a string equals the value at the path
            hit = cloudmap_filter(
                "/metadata/homepage_url=https://unfurl.cloud/feb20a/dashboard"
            )
            assert list(hit["repositories"]) == [dashboard], hit
            assert cloudmap_filter("/metadata/homepage_url=https://nope.example.com") == {}

            # an array *contains* the value
            hit = cloudmap_filter(
                "/metadata/discovery/sources="
                "https://hub.docker.com/v2/repositories/bitnami/odoo/"
            )
            assert list(hit) == ["artifacts"], hit
            assert next(iter(hit["artifacts"])).startswith("pkg:oci/odoo"), hit
            assert (
                cloudmap_filter(
                    "/metadata/discovery/sources="
                    "https://hub.docker.com/v2/repositories/other/"
                )
                == {}
            )

            # a member of an object is addressed by its key in the path;
            # searching the object's values isn't supported (postgres can't
            # serve that from a GIN index, so neither backend does it)
            hit = cloudmap_filter(
                "/branches/main=4551885dfab39991cfdb958cb79fcb6aa282481d"
            )
            assert list(hit["repositories"]) == [dashboard], hit
            assert (
                cloudmap_filter(
                    "/branches/main=0000000000000000000000000000000000000000"
                )
                == {}
            )
            assert (
                cloudmap_filter("/branches=4551885dfab39991cfdb958cb79fcb6aa282481d")
                == {}
            ), "the object itself doesn't match one of its members"

            # `true` / `false` are JSON booleans, not the strings "true"/"false"
            hit = cloudmap_filter("/private=true")
            assert list(hit["repositories"]) == [dashboard], hit
            assert cloudmap_filter("/private=false") == {}

            # `filter` repeats: every occurrence must match (ANDed in the
            # rust fast-path's SQL, ANDed predicates in the Python fold).
            res = requests.get(
                cloudmap_url,
                params=[
                    ("filter", "/private=true"),
                    ("filter", "/branches/main=4551885dfab39991cfdb958cb79fcb6aa282481d"),
                ],
            )
            assert res.status_code == 200, res.text
            assert list(res.json()["result"]["repositories"]) == [dashboard]
            # each filter matches a record on its own, but none matches both
            res = requests.get(
                cloudmap_url,
                params=[
                    ("filter", "/private=true"),
                    (
                        "filter",
                        "/metadata/homepage_url=https://unfurl.cloud/onecommons/blueprints/odoo",
                    ),
                ],
            )
            assert res.status_code == 200, res.text
            assert res.json()["result"] == {}

            # `null` matches a null value — a typeRef map's members are null,
            # so the type name goes in the path
            hit = cloudmap_filter("/type/cloudmap.artifacts.GitLabPipeline=null")
            assert len(hit["artifacts"]) == 4, hit
            assert all(k.endswith(".gitlab-ci.yml") for k in hit["artifacts"]), hit
            assert cloudmap_filter("/type/cloudmap.artifacts.GitLabPipeline=x") == {}
            assert cloudmap_filter("/type=null") == {}, "the map isn't null itself"

            # numbers are compared as JSON numbers
            hit = cloudmap_filter("/metadata/version=0.1")
            assert len(hit["artifacts"]) == 2, hit
            assert cloudmap_filter("/metadata/version=0.2") == {}

            # ...so a quoted value doesn't match a number: `"0.1"` is a string
            assert cloudmap_filter('/metadata/version="0.1"') == {}

            # an array *literal* is an exact match, not a containment test
            std_repo = "git://unfurl.cloud/onecommons/std.git"
            hit = cloudmap_filter('/metadata/topics=["documentation","library"]')
            assert list(hit["repositories"]) == [std_repo], hit
            # a subset isn't equal, and neither is a different order
            assert cloudmap_filter('/metadata/topics=["library"]') == {}
            assert cloudmap_filter('/metadata/topics=["library","documentation"]') == {}
            # ...while a scalar still means "contains"
            hit = cloudmap_filter("/metadata/topics=library")
            assert list(hit["repositories"]) == [std_repo], hit

            # `^=` matches a prefix: of a scalar, or of any element of an array
            hit = cloudmap_filter(
                "/metadata/homepage_url^=https://unfurl.cloud/feb20a"
            )
            assert list(hit["repositories"]) == [dashboard], hit
            hit = cloudmap_filter("/metadata/topics^=doc")
            assert list(hit["repositories"]) == [std_repo], hit
            assert cloudmap_filter("/metadata/homepage_url^=https://nope") == {}
            # a number never matches a prefix, and LIKE metacharacters are
            # literal rather than wildcards
            assert cloudmap_filter("/metadata/version^=0.") == {}
            assert cloudmap_filter("/metadata/homepage_url^=https://%") == {}

            # a bare path (no "=") tests that the path exists
            hit = cloudmap_filter("/metadata/topics")
            assert list(hit["repositories"]) == [std_repo], hit
            # a null-valued member still counts as present
            assert "repositories" in cloudmap_filter("/contains"), "null counts"
            assert cloudmap_filter("/metadata/no_such_field") == {}
            assert cloudmap_filter("/nope/deeper") == {}

            # an object literal is rejected, and so is a malformed array
            for bad in ('/metadata={"title":"x"}', "/metadata/topics=[1,"):
                res = requests.get(cloudmap_url, params={"filter": bad})
                assert res.status_code == 400, f"{bad}: {res.text}"

            # a path that doesn't resolve never matches
            assert cloudmap_filter("/metadata/nope/deeper=anything") == {}

            # `filter` combines with `kind` (both have to match)
            res = requests.get(
                cloudmap_url,
                params={
                    "kind": "repositories",
                    "filter": "/metadata/homepage_url="
                    "https://unfurl.cloud/feb20a/dashboard",
                },
            )
            assert res.status_code == 200, res.text
            assert list(res.json()["result"]["repositories"]) == [dashboard], res.text
            res = requests.get(
                cloudmap_url,
                params={
                    "kind": "artifacts",
                    "filter": "/metadata/homepage_url="
                    "https://unfurl.cloud/feb20a/dashboard",
                },
            )
            assert res.status_code == 200, res.text
            assert res.json()["result"] == {}, "kind and query must both match"

            # Malformed filter → 400 from both handlers. (A filter with no
            # "=" isn't malformed any more -- it's an existence test.)
            res = requests.get(cloudmap_url, params={"filter": "//empty/segment"})
            assert res.status_code == 400, res.text

            # ----- GET /cloudmap?select=... (projection; rust and
            # Python must return byte-identical records since the
            # `unfurl.server.*` annotations are dropped unless
            # selected) -----

            # `$key` + a top-level property: exact record equality
            # across all server variants.
            res = requests.get(
                cloudmap_url,
                params={
                    "kind": "services",
                    "key": service_key,
                    "select": "/type,$key",
                },
            )
            assert res.status_code == 200, res.text
            assert res.json()["result"] == {
                "services": {
                    service_key: {
                        "type": {
                            "Odoo@unfurl.cloud/onecommons/blueprints/odoo": None
                        },
                        "$key": service_key,
                    }
                }
            }

            # Composes with the type filter.
            res = requests.get(
                cloudmap_url,
                params={
                    "type": "cloudmap.artifacts.GitLabPipeline",
                    "select": "$key",
                },
            )
            assert res.status_code == 200, res.text
            primary = res.json()["result"]
            assert list(primary) == ["artifacts"]
            assert primary["artifacts"] == {
                k: {"$key": k} for k in primary["artifacts"]
            }

            # Nested pointer keeps structure; unresolvable paths are
            # omitted.
            template_key = (
                "git://unfurl.cloud/onecommons/blueprints/odoo.git"
                "#:ensemble-template.yaml%23spec/service_template"
            )
            res = requests.get(
                cloudmap_url,
                params={
                    "kind": "artifacts",
                    "key": template_key,
                    "select": "/metadata/title,/no/such/path",
                },
            )
            assert res.status_code == 200, res.text
            assert res.json()["result"] == {
                "artifacts": {template_key: {"metadata": {"title": "Odoo"}}}
            }

            # ----- GET /cloudmap/facets (grouped counts; the rust
            # fast-path aggregates in SQL, the Python fallback folds the
            # loaded document — every count must agree with what the
            # same server's /cloudmap selects) -----
            facets_url = f"http://{HOST}:{port}/cloudmap/facets"

            # The rollup invariant, self-calibrating: each type bucket
            # (subtypes default on) equals the record count ?type= selects.
            res = requests.get(
                facets_url, params={"kind": "artifacts", "group_by": "type"}
            )
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["meta"] == {
                "group_by": "/type",
                "facets": [],
                "subtypes": True,
            }
            for type_name, entry in body["groups"].items():
                sel = requests.get(
                    cloudmap_url, params={"kind": "artifacts", "type": type_name}
                )
                assert sel.status_code == 200, sel.text
                selected = sel.json()["result"]
                count = sum(
                    len(records)
                    for records in selected.values()
                    if isinstance(records, dict)
                )
                assert entry["count"] == count, (
                    f"bucket for {type_name!r} must count what ?type= selects"
                )

            # subtypes=false counts exact declared names — calibrate
            # against the artifact records themselves.
            res = requests.get(cloudmap_url, params={"kind": "artifacts"})
            assert res.status_code == 200, res.text
            artifacts = res.json()["result"]["artifacts"]
            declared: dict = {}
            for record in artifacts.values():
                type_ref = record.get("type")
                if isinstance(type_ref, dict):
                    for name in type_ref:
                        declared[name] = declared.get(name, 0) + 1
            res = requests.get(
                facets_url,
                params={
                    "kind": "artifacts",
                    "group_by": "type",
                    "subtypes": "false",
                },
            )
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["meta"]["subtypes"] is False
            assert body["total"] == len(artifacts)
            assert {g: e["count"] for g, e in body["groups"].items()} == declared

            # Repeated `facet=` params (one composite, one simple): the
            # spelling both servers must accept — the rust side through
            # its repeated-key extractor, the Python side through
            # getlist. A single-member "composite" equals the simple
            # column, which pins the two column forms to each other.
            res = requests.get(
                facets_url,
                params=[
                    ("kind", "artifacts"),
                    ("group_by", "type"),
                    ("facet", "type,type"),
                    ("facet", "type"),
                    ("subtypes", "false"),
                ],
            )
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["meta"]["facets"] == [["/type", "/type"], ["/type"]]
            for group_key, entry in body["groups"].items():
                composite, simple = entry["facets"]
                assert simple == {group_key: entry["count"]}, entry
                assert composite == {
                    json.dumps(
                        [group_key, group_key],
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ): entry["count"]
                }, entry

            # A literal, for cross-variant byte-parity: exactly one
            # repository is private. Scoped to the primary file because
            # an unscoped request means "every indexed file" to the rust
            # fast-path, which also sees zz-alt-cloudmap.yaml's
            # repository (see the /graph request above).
            res = requests.get(
                facets_url,
                params={
                    "kind": "repositories",
                    "group_by": "private",
                    "cloudmap_path": "cloudmap.yaml",
                },
            )
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["groups"] == {"true": {"count": 1}}
            assert body["total"] == 4

            # Error parity: a missing group_by violates the schema (422);
            # an empty path segment is a bad request (400).
            res = requests.get(facets_url, params={"kind": "artifacts"})
            assert res.status_code == 422, res.text
            res = requests.get(facets_url, params={"group_by": "a//b"})
            assert res.status_code == 400, res.text

            # ----- POST /cloudmap end-to-end -----
            cloudmap_path = os.path.abspath("cloudmap.yaml")

            # 1. Upsert: rewrite an existing repository entry. The
            #    fixture's `repository` schema requires `path`, so
            #    include it alongside `name`.
            existing_key = "git://unfurl.cloud/onecommons/std.git"
            res = requests.post(
                cloudmap_url,
                json={
                    "repositories": {
                        existing_key: {
                            "path": "onecommons/std",
                            "name": "renamed-via-post",
                        }
                    }
                },
            )
            assert res.status_code == 200, res.text
            response = res.json()
            # Both paths report the repository's current HEAD in ``commit``.
            # They differ in whether this request moved it: the python YAML
            # handler (including the queue-rust passthrough) commits
            # synchronously, while the rust local handler stages the record
            # in-flight and answers with the unchanged HEAD.
            assert isinstance(response.get("commit"), str), response
            assert response["commit"], "commit oid should be non-empty"
            if not rust_cloudmap_local:
                on_disk = Path(cloudmap_path).read_text()
                assert "renamed-via-post" in on_disk
            # GET-after-upsert: both paths reflect the change. GET
            # returns ``{"result": ..., "followed": ...}``; result is a
            # CloudMap-shaped doc keyed by section -> key -> record.
            read_back = requests.get(
                cloudmap_url,
                params={"kind": "repositories", "key": existing_key},
            )
            assert read_back.status_code == 200, read_back.text
            primary = read_back.json()["result"]["repositories"][existing_key]
            assert primary.get("name") == "renamed-via-post"

            # 1b. A `components` POST + GET round trip. Every layer keeps
            #     its own list of cloudmap sections and `components` used to
            #     be missing from several: python rejected the POST with
            #     "unknown section" and the rust handler collected no write
            #     ops for it (a silent no-op).
            component_key = "software.PostgresSchema@example.org"
            res = requests.post(
                cloudmap_url,
                json={
                    "components": {
                        component_key: {
                            # `version` is a *string* here, which the query
                            # assertions below use to pin the quoting rule.
                            "metadata": {"title": "App schema", "version": "42"}
                        }
                    }
                },
            )
            assert res.status_code == 200, res.text
            read_back = requests.get(
                cloudmap_url,
                params={"kind": "components", "key": component_key},
            )
            assert read_back.status_code == 200, read_back.text
            component = read_back.json()["result"]["components"][component_key]
            assert component["metadata"]["title"] == "App schema", component

            # A quoted value forces a string comparison, so it matches the
            # component posted just above; the bare form is the number 42 and
            # matches nothing.
            res = requests.get(
                cloudmap_url, params={"filter": '/metadata/version="42"'}
            )
            assert res.status_code == 200, res.text
            assert list(res.json()["result"]["components"]) == [component_key], res.text
            res = requests.get(cloudmap_url, params={"filter": "/metadata/version=42"})
            assert res.status_code == 200, res.text
            assert res.json()["result"] == {}, res.text

            # 2. Delete via `unfurl.server.deleted: true`.
            import yaml as _yaml

            delete_key = "git://unfurl.cloud/feb20a/dashboard.git"
            res = requests.post(
                cloudmap_url,
                json={
                    "repositories": {
                        delete_key: {"unfurl.server.deleted": True}
                    }
                },
            )
            assert res.status_code == 200, res.text
            response = res.json()
            # Same as the upsert: ``commit`` is the repository's HEAD on both
            # paths, moved by python's synchronous commit and unchanged by the
            # rust handler's in-flight staging.
            assert isinstance(response.get("commit"), str), response
            # GET-after-delete: both paths return 404 for the gone key.
            read_back = requests.get(
                cloudmap_url,
                params={"kind": "repositories", "key": delete_key},
            )
            assert read_back.status_code == 404, read_back.text
            if not rust_cloudmap_local:
                # Python YAML path commits the delete synchronously, so
                # the file on disk no longer mentions the key.
                on_disk_doc = _yaml.safe_load(Path(cloudmap_path).read_text())
                assert delete_key not in on_disk_doc.get("repositories", {})

            # 3. Unknown section → 400. Both Python and the rust local
            #    handler explicitly check unknown top-level keys before
            #    applying (rust inspects the typed request's
            #    `additional_properties` flatten bag, mirroring
            #    Pydantic's `extra="allow"` + manual section check).
            res = requests.post(cloudmap_url, json={"flarp": {}})
            assert res.status_code == 400, res.text

            # 3b. ...but `branch` is an envelope key, not a section. Both
            #     handlers accept it: Python resolves the clone with it,
            #     and the rust local handler routes on it (a write naming
            #     a branch other than the one its worktree is checked out
            #     on is proxied). An envelope-only body is a no-op, so
            #     this asserts acceptance without writing anything.
            res = requests.post(cloudmap_url, json={"branch": repo.active_branch})
            assert res.status_code == 200, res.text

            # 4. Schema-violating record → 422. The repository schema
            #    requires `protocols` to be an array of strings.
            res = requests.post(
                cloudmap_url,
                json={
                    "repositories": {
                        existing_key: {
                            "path": "onecommons/std",
                            "protocols": "not-an-array",
                        }
                    }
                },
            )
            assert res.status_code == 422, (
                f"expected 422 schema violation, got {res.status_code}: {res.text}"
            )
            if not rust_cloudmap_local:
                # APIFlask wraps Pydantic validation errors under
                # `detail.json._schema`; the message includes
                # 'cloudmap schema violation' from our model_validator.
                assert "cloudmap schema violation" in res.text

            # 5. cloudmap_path round-trip: the second cloudmap seeded above
            #    must be reachable through its own path, and must not leak
            #    into the default file's view. Before `cloudmap_path` was
            #    honored by the reads, a POST wrote to the alt file while
            #    both GETs kept serving `cloudmap.yaml`.
            res = requests.get(
                cloudmap_url,
                params={"kind": "repositories", "cloudmap_path": alt_path},
            )
            assert res.status_code == 200, res.text
            assert list(res.json()["result"]["repositories"]) == [alt_key], res.text

            res = requests.get(
                cloudmap_url,
                params={"kind": "repositories", "cloudmap_path": "cloudmap.yaml"},
            )
            assert res.status_code == 200, res.text
            assert alt_key not in res.json()["result"]["repositories"], (
                "the cloudmap.yaml view must not include the alt file"
            )

            # POST to the alt path, then read it back through the same path
            res = requests.post(
                cloudmap_url,
                json={
                    "cloudmap_path": alt_path,
                    "repositories": {
                        alt_key: {"path": "only/in-alt", "name": "renamed-in-alt"}
                    },
                },
            )
            assert res.status_code == 200, res.text
            res = requests.get(
                cloudmap_url,
                params={
                    "kind": "repositories",
                    "key": alt_key,
                    "cloudmap_path": alt_path,
                },
            )
            assert res.status_code == 200, res.text
            assert res.json()["result"]["repositories"][alt_key]["name"] == "renamed-in-alt"

            # /graph honors it too
            res = requests.get(
                f"http://{HOST}:{port}/graph", params={"cloudmap_path": alt_path}
            )
            assert res.status_code == 200, res.text

            # 6. POST to a cloudmap_path that doesn't exist yet creates the
            #    file (in a subdirectory it also has to create) and adds it
            #    to the repository, rather than failing to load it.
            new_path = "maps/new-cloudmap.yaml"
            new_url = "git://example.com/in-brand-new-file.git"
            res = requests.post(
                cloudmap_url,
                json={
                    "cloudmap_path": new_path,
                    "repositories": {
                        new_url: {"path": "in/brand-new", "name": "brand-new"}
                    },
                },
            )
            assert res.status_code == 200, res.text
            if not rust_cloudmap_local:
                created = Path(os.path.abspath(new_path))
                assert created.is_file(), "the new cloudmap should be on disk"
                created_doc = _yaml.safe_load(created.read_text())
                # `apiVersion` + `kind` are both required by
                # unfurl/cloudmap/cloudmap-schema.json, and `kind` is what
                # identifies the file as a cloudmap to the rust scanner.
                assert created_doc["apiVersion"] == "unfurl/v1.0.0"
                assert created_doc["kind"] == "CloudMap"
                assert new_url in created_doc["repositories"]
                # committed, not just written
                assert new_path in repo.run_cmd(["ls-files", new_path])[1]
            # 6b. A stale `latest_commit` is refused with 409 by both
            #     handlers: it pins the write to the repository revision the
            #     client last saw.
            res = requests.post(
                cloudmap_url,
                json={
                    "latest_commit": "0" * 40,
                    "repositories": {
                        existing_key: {"path": "onecommons/std", "name": "stale-occ"}
                    },
                },
            )
            assert res.status_code == 409, res.text
            assert "latest_commit" in res.text

            # ... while the current revision is accepted.
            res = requests.post(
                cloudmap_url,
                json={
                    "latest_commit": repo.revision,
                    "repositories": {
                        existing_key: {"path": "onecommons/std", "name": "fresh-occ"}
                    },
                },
            )
            assert res.status_code == 200, res.text

            # 7. `commit: false` writes without committing; a later
            #    `commit: true` (with no records at all) commits it.
            head_before = repo.revision
            res = requests.post(
                cloudmap_url,
                json={
                    "commit": False,
                    "repositories": {
                        existing_key: {"path": "onecommons/std", "name": "uncommitted"}
                    },
                },
            )
            assert res.status_code == 200, res.text
            assert res.json()["commit"] == head_before, (
                "commit=false reports the unchanged HEAD"
            )
            assert repo.revision == head_before, "commit=false must not move HEAD"
            if not rust_cloudmap_local:
                assert "uncommitted" in Path(cloudmap_path).read_text(), (
                    "commit=false should still write the file"
                )
                assert repo.is_dirty(True, cloudmap_path), "left dirty for a later commit"

            res = requests.post(
                cloudmap_url,
                json={"commit": True, "commit_msg": "commit the pending write"},
            )
            assert res.status_code == 200, res.text
            assert res.json()["commit"], res.text
            assert repo.revision != head_before, "commit=true should move HEAD"
            assert not repo.is_dirty(True, cloudmap_path), "working tree is clean now"
            assert (
                "commit the pending write"
                in repo.run_cmd(["log", "-1", "--format=%s"])[1]
            )

            # An empty commit=true against a clean tree is a no-op.
            head_after = repo.revision
            res = requests.post(cloudmap_url, json={"commit": True})
            assert res.status_code == 200, res.text
            assert repo.revision == head_after, "nothing dirty -> no new commit"

            # readable back through the same path
            res = requests.get(
                cloudmap_url,
                params={
                    "kind": "repositories",
                    "key": new_url,
                    "cloudmap_path": new_path,
                },
            )
            assert res.status_code == 200, res.text
            assert res.json()["result"]["repositories"][new_url]["name"] == "brand-new"

            # 8. `unfurl.server.merge` merges into the existing record and
            #    `unfurl.server.if_exists` skips a record that doesn't exist.
            before_merge = requests.get(
                cloudmap_url, params={"kind": "repositories", "key": existing_key}
            ).json()["result"]["repositories"][existing_key]
            missing_key = "git://example.com/no-such-repo.git"
            res = requests.post(
                cloudmap_url,
                json={
                    "commit": True,
                    "repositories": {
                        existing_key: {
                            "status": "archived",
                            "unfurl.server.merge": True,
                            "unfurl.server.if_exists": True,
                        },
                        missing_key: {
                            "status": "deleted",
                            "unfurl.server.merge": True,
                            "unfurl.server.if_exists": True,
                        },
                    },
                },
            )
            assert res.status_code == 200, res.text
            assert [r["key"] for r in res.json()["applied"]] == [existing_key], res.text
            merged = requests.get(
                cloudmap_url, params={"kind": "repositories", "key": existing_key}
            ).json()["result"]["repositories"][existing_key]
            assert merged["status"] == "archived"
            for field in ("path", "name"):
                assert merged[field] == before_merge[field], merged
            assert (
                requests.get(
                    cloudmap_url, params={"kind": "repositories", "key": missing_key}
                ).status_code
                == 404
            )
            # a merge directive can delete fields
            res = requests.post(
                cloudmap_url,
                json={
                    "commit": True,
                    "repositories": {
                        existing_key: {"unfurl.server.merge": {"delete": ["status"]}}
                    },
                },
            )
            assert res.status_code == 200, res.text
            unmerged = requests.get(
                cloudmap_url, params={"kind": "repositories", "key": existing_key}
            ).json()["result"]["repositories"][existing_key]
            assert "status" not in unmerged, unmerged
            assert unmerged["path"] == before_merge["path"]
            res = requests.post(
                cloudmap_url,
                json={
                    "repositories": {
                        existing_key: {"unfurl.server.merge": {"strategy": "replace"}}
                    }
                },
            )
            assert res.status_code == 400, res.text
            # merge without if_exists creates a record that's missing
            res = requests.post(
                cloudmap_url,
                json={
                    "commit": True,
                    "repositories": {
                        missing_key: {"path": "no-such-repo", "unfurl.server.merge": True}
                    },
                },
            )
            assert res.status_code == 200, res.text
            assert [r["key"] for r in res.json()["applied"]] == [missing_key], res.text
            created = requests.get(
                cloudmap_url, params={"kind": "repositories", "key": missing_key}
            ).json()["result"]["repositories"][missing_key]
            assert created["path"] == "no-such-repo"
            if not rust_cloudmap_local:
                on_disk_doc = _yaml.safe_load(Path(cloudmap_path).read_text())
                on_disk = on_disk_doc["repositories"][existing_key]
                # merged, then its status deleted again by the merge directive
                assert "status" not in on_disk and on_disk["path"] == before_merge["path"]
                assert "unfurl.server.merge" not in on_disk

            # 8b. a write takes over the fields it changes from the analysis
            #     that wrote them (metadata.discovery.applied), unless it
            #     says it maintains `applied` itself
            owned_key = "git://example.com/owned.git"

            def owned(description, path="owned", **markers):
                record = {
                    "path": path,
                    "metadata": {
                        "description": description,
                        "discovery": {
                            "applied": {
                                owned_key: ["metadata/description", "path"]
                            }
                        },
                    },
                    **markers,
                }
                res = requests.post(
                    cloudmap_url,
                    json={"commit": True, "repositories": {owned_key: record}},
                )
                assert res.status_code == 200, res.text
                res = requests.get(
                    cloudmap_url, params={"kind": "repositories", "key": owned_key}
                )
                assert res.status_code == 200, res.text
                record = res.json()["result"]["repositories"][owned_key]
                return record["metadata"]["discovery"].get("applied")

            keep = {"unfurl.server.keep_applied": True}
            assert owned("a", **keep) == {
                owned_key: ["metadata/description", "path"]
            }
            assert owned("b") == {owned_key: ["path"]}
            assert owned("b", path="moved", **keep) == {
                owned_key: ["metadata/description", "path"]
            }

            # 9. POST /cloudmap/analyze mirrors `unfurl cloudmap --add`; a
            #    service url produces a record without touching the network.
            analyze_url = f"http://{HOST}:{port}/cloudmap/analyze"
            service_url = "https://analyze.example.com/app"
            head_before = repo.revision
            res = requests.post(analyze_url, json={"add": [service_url]})
            assert res.status_code == 200, res.text
            assert res.json()["added"] == [
                {"url": service_url, "section": "services", "key": service_url}
            ], res.text
            assert res.json()["skipped"] == []
            assert repo.revision != head_before, "analyze commits by default"
            assert res.json()["commit"] == repo.revision
            read_back = requests.get(
                cloudmap_url, params={"kind": "services", "key": service_url}
            )
            assert read_back.status_code == 200, read_back.text
            if not rust_cloudmap_local:
                on_disk_doc = _yaml.safe_load(Path(cloudmap_path).read_text())
                assert service_url in on_disk_doc["services"]
                # the rest of the document is untouched
                assert existing_key in on_disk_doc["repositories"]

            # already present: skipped, as `--add` does without --analyze yes
            head_before = repo.revision
            res = requests.post(analyze_url, json={"add": [service_url]})
            assert res.status_code == 200, res.text
            assert res.json()["added"] == [] and res.json()["skipped"] == [service_url]
            assert repo.revision == head_before, "nothing added -> no commit"

            # replace re-analyzes it
            res = requests.post(
                analyze_url,
                json={"replace": [service_url], "commit_msg": "re-analyze the service"},
            )
            assert res.status_code == 200, res.text
            assert [r["key"] for r in res.json()["added"]] == [service_url]

            # the server's own filesystem is off limits
            res = requests.post(analyze_url, json={"add": ["file:///etc"]})
            assert res.status_code == 400, res.text
            res = requests.post(analyze_url, json={})
            assert res.status_code == 400, res.text

            # deleted / private / moved update the records, never remove them
            types_key = "git://unfurl.cloud/onecommons/unfurl-types.git"
            odoo_key = "git://unfurl.cloud/onecommons/blueprints/odoo.git"
            std_key = "git://unfurl.cloud/onecommons/std.git"
            res = requests.post(
                analyze_url,
                json={
                    "deleted": [
                        "https://unfurl.cloud/onecommons/unfurl-types.git",
                        "https://example.com/never-added.git",
                    ],
                    "private": ["https://unfurl.cloud/onecommons/blueprints/odoo.git"],
                    "moved": [
                        {
                            "from": "https://unfurl.cloud/onecommons/std.git",
                            "to": "https://unfurl.cloud/onecommons/std-lib.git",
                        }
                    ],
                },
            )
            assert res.status_code == 200, res.text
            assert sorted(r["key"] for r in res.json()["added"]) == sorted(
                [types_key, odoo_key, std_key]
            ), res.text
            assert res.json()["skipped"] == ["https://example.com/never-added.git"]

            def repository(key):
                res = requests.get(cloudmap_url, params={"kind": "repositories", "key": key})
                assert res.status_code == 200, res.text
                return res.json()["result"]["repositories"][key]

            assert repository(types_key)["status"] == "deleted"
            assert repository(odoo_key)["private"] is True
            std = repository(std_key)
            assert std["status"] == "moved"
            assert std["moved_to"] == "git://unfurl.cloud/onecommons/std-lib.git"
            # nothing left to change the second time
            res = requests.post(
                analyze_url,
                json={"deleted": ["https://unfurl.cloud/onecommons/unfurl-types.git"]},
            )
            assert res.status_code == 200, res.text
            assert res.json()["added"] == []
        finally:
            _terminate_process(p)


@pytest.mark.parametrize("server_env", server_env)
def test_cloudmap_proxy_round_trip(server_env):
    """End-to-end CloudMapProxy round-trip against every server variant.

    Runs against the four server flavours from :data:`server_env`:

    - ``no-redis`` / ``redis``: pure Python server. The /cloudmap
      handler reads / writes ``cloudmap.yaml`` directly. No
      ``unfurl.server.{id,version,commit}`` annotations on records;
      ``since_version`` / ``exclude`` are accepted but ignored. Each
      successful POST produces a real git commit oid in the response.
    - ``redis-rust``: rust proxy in front of Python, with the rust
      cloudmap fast-path enabled (``UNFURL_CLOUDMAP_REPO`` +
      ``UNFURL_CLOUDMAP_DB_URL``). Records carry the OCC + id
      annotations; POST stages an in-flight write (commit=None,
      queueid bumped).
    """
    from pathlib import Path
    from unfurl.cloudmap.proxy import CloudMapProxy
    from unfurl.tosca_plugins.cloudmap_defs import (
        Artifact,
        ArtifactMetadata,
    )

    if server_env == "queue-rust":
        pytest.skip(
            "cloudmap endpoints on a server using git-sync doesn't queue writes, so this test is redundant."
        )

    is_rust = "rust" in server_env

    fixture_dir = Path(__file__).parent / "fixtures"
    cloudmap_content = (fixture_dir / "expected_cloudmap.yaml").read_text()

    runner = CliRunner()
    port = _next_port()
    with runner.isolated_filesystem() as tmpdir:
        # cloudmap repo: a real git worktree with cloudmap.yaml.
        cloudmap_path = os.path.abspath("cloudmap.yaml")
        with open(cloudmap_path, "w") as f:
            f.write(cloudmap_content)
        repo = GitRepo(Repo.init("."))
        repo.add_all(os.path.abspath("."))
        repo.commit_files([cloudmap_path], "Add cloudmap")

        extra_env = _env_for(server_env, "cloudmap-proxy")
        if is_rust:
            # sqlite db file for the rust SyncedRepo backend.
            extra_env["UNFURL_CLOUDMAP_REPO"] = os.path.abspath(".")
            extra_env["UNFURL_CLOUDMAP_DB_URL"] = _cloudmap_db_url()

        ctx = get_context("spawn")
        error_queue = ctx.Queue()
        p = ctx.Process(
            target=serve_server,
            args=(HOST, port, None, ".", f"{tmpdir}", {"home": ""}),
            kwargs={
                "error_queue": error_queue,
                "extra_env": extra_env,
            },
        )
        p._error_queue = error_queue
        try:
            start_server_process(p, port, is_rust=is_rust)

            base_url = f"http://{HOST}:{port}"
            proxy = CloudMapProxy(base_url)

            # find_repositories triggers a per-section fetch and returns
            # an iterator (not a list) — paging-ready.
            repos_iter = proxy.find_repositories()
            assert iter(repos_iter) is repos_iter
            repos = list(repos_iter)
            assert repos, "expected fixture cloudmap to have repositories"

            # OCC tokens are only stamped on the rust path; the
            # Python YAML fallback returns records without them.
            if is_rust:
                assert proxy._cache._max_version > 0
            else:
                assert proxy._cache._max_version == 0

            # Second find_repositories call is local-only (no HTTP).
            list(proxy.find_repositories())
            assert "repositories" in proxy._cache._section_loaded

            # find_artifacts is a separate fetch.
            artifacts = list(proxy.find_artifacts())
            assert artifacts, "expected fixture cloudmap to have artifacts"

            # get_artifact for one we already have is a cache hit.
            first_url = artifacts[0].url
            assert proxy.get_artifact(first_url) is artifacts[0]

            # Stage a new artifact and save().
            initial_max = proxy._cache._max_version
            new = Artifact(
                url="pkg:oci/proxy-test/new@1.0",
                metadata=ArtifactMetadata(title="proxy-injected"),
            )
            proxy.add_record(new)
            commit_oid = proxy.save()

            if is_rust:
                # Rust local handler stages to its in-flight db: `commit`
                # reports the repository's unchanged HEAD (nothing was
                # committed), and the response's queueid (== largest
                # unfurl.server.version stamped) is folded into the cache's
                # _max_version, which is the OCC token for a staged write.
                assert isinstance(commit_oid, str) and commit_oid
                assert proxy._cache._max_version > initial_max
                cached_record = proxy.get_artifact(new.url)
                assert cached_record is not None
                assert (
                    cached_record._unfurl_server_version
                    == proxy._cache._max_version
                )
                # In-flight: no commit yet.
                assert cached_record._unfurl_server_commit is None
            else:
                # Python handler commits to git and returns the oid.
                assert isinstance(commit_oid, str) and commit_oid
                # _max_version doesn't advance — Python doesn't stamp
                # version tokens on records.
                assert proxy._cache._max_version == 0

            # Refresh: server returns the latest. With no since_version
            # filter (Python ignores it; rust's since=0 returns
            # everything > 0), at minimum the cache stays consistent.
            after_save_max = proxy._cache._max_version
            proxy.refresh()
            assert proxy._cache._max_version >= after_save_max

            # The new artifact should now be retrievable from a fresh
            # proxy instance — tests the server-side persistence.
            proxy2 = CloudMapProxy(base_url)
            fetched = proxy2.get_artifact(new.url)
            assert fetched is not None
            assert fetched.url == new.url
        finally:
            _terminate_process(p)


def test_default_cloudmap_resolution(monkeypatch):
    """A request naming no project reads the rust server's worktree when there
    is one, else the public cloudmap; a server not started by the cli refuses
    to write without one either way."""
    import flask

    monkeypatch.delenv("UNFURL_SERVE_PATH", raising=False)
    monkeypatch.setitem(server.app.config, "UNFURL_LOCAL_ENV", None)
    monkeypatch.setitem(server.app.config, "UNFURL_CURRENT_CLOUDMAP", None)

    def resolve(query, rust_url=None):
        monkeypatch.setitem(server.app.config, "UNFURL_LOCAL_CLOUDMAP_URL", rust_url)
        with server.app.test_request_context(query_string=query):
            return server_cloudmap._cloudmap_project_id(flask.request)

    rust_url = "http://127.0.0.1:8081"
    assert resolve("auth_project=me/proj", rust_url) == "me/proj"
    assert resolve("", rust_url) == ""  # get_cloudmap_proxy sends it there
    assert resolve("") == server_cloudmap.CLOUDMAP_PROJECT

    client = server.app.test_client()
    for url in (None, rust_url):
        monkeypatch.setitem(server.app.config, "UNFURL_LOCAL_CLOUDMAP_URL", url)
        res = client.post("/cloudmap", json={"repositories": {}})
        assert res.status_code == 400, res.json
        assert "auth_project" in res.json["message"]


def test_cli_server_uses_its_local_environments_cloudmap(monkeypatch, tmp_path):
    """A server started by the cli with no rust worktree reads and writes the
    cloudmap its local environment configures."""
    import yaml as pyyaml
    from unfurl.localenv import LocalEnv

    project = tmp_path / "project"
    project.mkdir()
    cloudmap_file = project / "maps" / "cloudmap.yaml"
    cloudmap_file.parent.mkdir()
    cloudmap_file.write_text(
        pyyaml.safe_dump(
            {
                "apiVersion": "unfurl/v1.0.0",
                "kind": "CloudMap",
                "repositories": {
                    "git://example.com/org/a.git": {"name": "a", "path": "org/a"}
                },
            }
        )
    )
    (project / "unfurl.yaml").write_text(
        pyyaml.safe_dump(
            {
                "apiVersion": "unfurl/v1.0.0",
                "kind": "Project",
                "environments": {
                    "defaults": {
                        "cloudmaps": {
                            "repositories": {"cloudmap": {"url": str(cloudmap_file)}}
                        }
                    }
                },
            }
        )
    )
    # the served directory's own cloudmap, which the configured one overrides
    (project / "cloudmap.yaml").write_text(
        pyyaml.safe_dump({"apiVersion": "unfurl/v1.0.0", "kind": "CloudMap"})
    )
    repo = Repo.init(project)
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "test@example.com")
        cw.set_value("user", "name", "test")
    repo.git.add(A=True)
    repo.git.commit("-m", "init")

    monkeypatch.setenv("UNFURL_SERVE_PATH", str(project))
    monkeypatch.setitem(server.app.config, "UNFURL_LOCAL_CLOUDMAP_URL", None)
    monkeypatch.setitem(server.app.config, "UNFURL_CURRENT_WORKING_DIR", str(project))
    monkeypatch.setitem(
        server.app.config,
        "UNFURL_LOCAL_ENV",
        LocalEnv(str(project), homePath="", can_be_empty=True),
    )
    monkeypatch.setitem(server.app.config, "UNFURL_CURRENT_CLOUDMAP", None)
    monkeypatch.setitem(server.app.config, "UNFURL_LOCAL_PROJECTS", {})
    monkeypatch.setitem(server.app.config, "CACHE_DEFAULT_PULL_TIMEOUT", 120)
    monkeypatch.setitem(server.app.config, "UNFURL_CLOUD_SERVER", "https://unfurl.cloud")
    monkeypatch.setitem(server.app.config, "UNFURL_CLONE_ROOT", str(tmp_path / "clones"))
    client = server.app.test_client()

    # resolving it is locked like cloning a project: a request that finds it
    # locked fails rather than using another cloudmap
    lock = tmp_path / "clones" / ".cloudmap.lock"
    lock.parent.mkdir()
    lock.write_text("1")
    res = client.get("/cloudmap?kind=repositories")
    assert res.status_code == 500, res.get_data(as_text=True)
    assert res.json["code"] == "BAD_REPOSITORY"
    lock.unlink()

    res = client.get("/cloudmap?kind=repositories")
    assert res.status_code == 200, res.get_data(as_text=True)
    assert "git://example.com/org/a.git" in res.json["result"]["repositories"]
    assert not lock.exists()

    commits = len(list(repo.iter_commits()))
    res = client.post(
        "/cloudmap",
        json={"repositories": {"git://example.com/org/b.git": {"name": "b", "path": "org/b"}}},
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    assert "git://example.com/org/b.git" in pyyaml.safe_load(cloudmap_file.read_text())[
        "repositories"
    ]
    assert len(list(repo.iter_commits())) == commits + 1
    assert res.json["commit"] == repo.head.commit.hexsha

    # analysis saves through the CloudMap, also as one commit
    res = client.post(
        "/cloudmap/analyze",
        json={"add": ["pkg:generic/example@1.0"], "analyze": "no"},
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    assert res.json["added"], res.json
    assert len(list(repo.iter_commits())) == commits + 2
    assert res.json["commit"] == repo.head.commit.hexsha
    assert "artifacts" in pyyaml.safe_load(cloudmap_file.read_text())


def test_cli_server_without_a_configured_cloudmap_uses_its_own(monkeypatch, tmp_path):
    """A server started by the cli whose local environment configures no
    cloudmap reads the one in the project it serves, rather than
    ``CloudMap.from_name``'s default."""
    import yaml as pyyaml
    from unfurl.cloudmap import CloudMap
    from unfurl.localenv import LocalEnv

    (tmp_path / "unfurl.yaml").write_text(
        pyyaml.safe_dump({"apiVersion": "unfurl/v1.0.0", "kind": "Project"})
    )
    (tmp_path / "cloudmap.yaml").write_text(
        pyyaml.safe_dump(
            {
                "apiVersion": "unfurl/v1.0.0",
                "kind": "CloudMap",
                "repositories": {
                    "git://example.com/org/own.git": {"name": "own", "path": "org/own"}
                },
            }
        )
    )
    repo = Repo.init(tmp_path)
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "test@example.com")
        cw.set_value("user", "name", "test")
    repo.git.add(A=True)
    repo.git.commit("-m", "init")

    def from_name(*args, **kw):
        raise AssertionError("resolved a default cloudmap")

    monkeypatch.setattr(CloudMap, "from_name", from_name)
    monkeypatch.setenv("UNFURL_SERVE_PATH", str(tmp_path))
    monkeypatch.setitem(server.app.config, "UNFURL_LOCAL_CLOUDMAP_URL", None)
    monkeypatch.setitem(server.app.config, "UNFURL_CURRENT_WORKING_DIR", str(tmp_path))
    monkeypatch.setitem(
        server.app.config,
        "UNFURL_LOCAL_ENV",
        LocalEnv(str(tmp_path), homePath="", can_be_empty=True),
    )
    monkeypatch.setitem(server.app.config, "UNFURL_CURRENT_CLOUDMAP", None)
    monkeypatch.setitem(server.app.config, "CACHE_DEFAULT_PULL_TIMEOUT", 120)
    res = server.app.test_client().get("/cloudmap?kind=repositories")
    assert res.status_code == 200, res.get_data(as_text=True)
    assert "git://example.com/org/own.git" in res.json["result"]["repositories"]


def test_default_rust_worktree_is_forwarded_to(monkeypatch):
    """Without a project, reads and writes are forwarded to the rust server's
    worktree with the request's own parameters."""
    from unfurl.cloudmap.proxy import CloudMapProxy

    monkeypatch.delenv("UNFURL_SERVE_PATH", raising=False)
    monkeypatch.setitem(
        server.app.config, "UNFURL_LOCAL_CLOUDMAP_URL", "http://127.0.0.1:8081"
    )
    sent = []

    class Answer:
        content = b'{"forwarded": true}'
        status_code = 200
        headers = {"Content-Type": "application/json"}

    def forward(self, method, path, params, body=None):
        sent.append((method, path, params, body))
        return Answer()

    monkeypatch.setattr(CloudMapProxy, "forward", forward)
    client = server.app.test_client()
    res = client.get("/cloudmap/facets?group_by=type&facet=type")
    assert res.json == {"forwarded": True}
    # a server started by the cli writes without a project too
    monkeypatch.setenv("UNFURL_SERVE_PATH", ".")
    res = client.post("/cloudmap", json={"repositories": {}})
    assert res.json == {"forwarded": True}
    assert sent == [
        ("GET", "/facets", [("group_by", "type"), ("facet", "type")], None),
        ("POST", "", [], {"repositories": {}}),
    ]


def test_refreshing_the_local_env_updates_the_current_cloudmap(monkeypatch):
    """The saved LocalEnv, and the default cloudmap resolved from it, follow a
    refresh."""
    from types import SimpleNamespace

    refreshed = object()
    default = SimpleNamespace(local_env=object())
    monkeypatch.setattr(server, "set_current_ensemble_git_url", lambda gui: refreshed)
    monkeypatch.setitem(server.app.config, "UNFURL_GUI_MODE", None)
    monkeypatch.setitem(server.app.config, "UNFURL_LOCAL_ENV", None)
    monkeypatch.setitem(server.app.config, "UNFURL_CURRENT_CLOUDMAP", default)
    server.refresh_current_localenv()
    assert server.app.config["UNFURL_LOCAL_ENV"] is refreshed
    assert default.local_env is refreshed


def test_default_upstream_cloudmap_is_forwarded_to(monkeypatch, tmp_path):
    """Without a project, a server whose local environment configures an
    upstream cloudmap server forwards to it, with the credentials configured
    for it, through a new proxy for each request."""
    import yaml as pyyaml
    from unfurl.cloudmap.proxy import CloudMapProxy
    from unfurl.localenv import LocalEnv

    upstream = {
        "url": "https://upstream.example/?auth_project=org/cloudmap",
        "username": "user",
        "password": "token",
    }
    (tmp_path / "unfurl.yaml").write_text(
        pyyaml.safe_dump(
            {
                "apiVersion": "unfurl/v1.0.0",
                "kind": "Project",
                "environments": {
                    "defaults": {"cloudmaps": {"servers": {"cloudmap": upstream}}}
                },
            }
        )
    )
    monkeypatch.setenv("UNFURL_SERVE_PATH", str(tmp_path))
    monkeypatch.setitem(server.app.config, "UNFURL_LOCAL_CLOUDMAP_URL", None)
    monkeypatch.setitem(
        server.app.config,
        "UNFURL_LOCAL_ENV",
        LocalEnv(str(tmp_path), homePath="", can_be_empty=True),
    )
    monkeypatch.setitem(server.app.config, "UNFURL_CURRENT_CLOUDMAP", None)
    sent = []

    class Answer:
        content = b'{"forwarded": true}'
        status_code = 200
        headers = {"Content-Type": "application/json"}

    def forward(self, method, path, params, body=None):
        sent.append((self, self._endpoint, self._base_query, self._headers()))
        return Answer()

    monkeypatch.setattr(CloudMapProxy, "forward", forward)
    client = server.app.test_client()
    for _ in range(2):
        res = client.get("/cloudmap?kind=repositories")
        assert res.json == {"forwarded": True}
    (first, endpoint, query, headers), (second, *_) = sent
    assert first is not second
    assert endpoint == "https://upstream.example/cloudmap"
    assert query == [("auth_project", "org/cloudmap")]
    assert "X-Git-Credentials" in headers
    # so pushes are analyzed into it too
    with server.app.app_context():
        assert server_cloudmap._proxied_default()
