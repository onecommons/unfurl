"""Safe mode must not let a checkout or an import reach outside the project.

Two escapes are covered here:

* a repository with a committed symlink pointing outside itself -- cloning
  or pulling it in safe mode would otherwise make an arbitrary file
  readable through the checked-out tree;
* a `file:` url or bare path in a repository definition that resolves
  outside the project.
"""

import os
import subprocess

import pytest

# unfurl puts the vendored toscaparser on sys.path, so it must be imported first
from unfurl.repo import Repo
from unfurl.yamlloader import ImportResolver
from toscaparser.common.exception import ExceptionCollector


def _repo_with_symlink(path, target="/etc/passwd"):
    """A repo holding a symlink out of the tree, plus a regular file."""
    path.mkdir(parents=True, exist_ok=True)

    def run(*a):
        subprocess.run(a, cwd=str(path), check=True, capture_output=True)

    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "test@example.com")
    run("git", "config", "user.name", "Test")
    (path / "real.txt").write_text("hello\n")
    os.symlink(target, str(path / "escape.txt"))
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", "initial")
    return str(path)


def test_clone_without_symlinks_checks_out_a_regular_file(tmp_path):
    src = _repo_with_symlink(tmp_path / "src")
    dest = str(tmp_path / "clone")

    repo = Repo.create_working_dir(src, dest, symlinks=False)

    escape = os.path.join(dest, "escape.txt")
    assert not os.path.islink(escape), "symlink should not have been created"
    # core.symlinks=false checks the link out as a file holding its target
    # path, so the target's *contents* are never exposed.
    assert open(escape).read() == "/etc/passwd"
    assert open(os.path.join(dest, "real.txt")).read() == "hello\n"
    # persisted, so a later checkout here can't reintroduce one: the clone
    # environment governs only the clone's own checkout
    assert repo.repo.git.config("--local", "--get", "core.symlinks") == "false"


def test_clone_with_symlinks_is_the_default(tmp_path):
    """The before-state: without the flag the escape is real.

    Keeps the test above honest -- if cloning never produced a symlink
    here, it would pass for the wrong reason.
    """
    src = _repo_with_symlink(tmp_path / "src")
    dest = str(tmp_path / "clone")

    Repo.create_working_dir(src, dest)

    escape = os.path.join(dest, "escape.txt")
    assert os.path.islink(escape)
    assert os.readlink(escape) == "/etc/passwd"


def test_pull_in_safe_mode_disables_symlinks(tmp_path, monkeypatch):
    """`pull()` persists core.symlinks=false, covering later checkouts too."""
    import tosca.loader
    from unfurl.server.serve import pull

    src_path = tmp_path / "src"
    src = _repo_with_symlink(src_path)
    dest = str(tmp_path / "clone")
    # cloned *before* safe mode, the way a long-lived server cache would be
    repo = Repo.create_working_dir(src, dest)
    assert os.path.islink(os.path.join(dest, "escape.txt"))

    # a second symlink lands upstream after the clone
    os.symlink("/etc/hosts", str(src_path / "escape2.txt"))
    for a in (
        ("git", "add", "-A"),
        ("git", "commit", "-q", "-m", "second"),
    ):
        subprocess.run(a, cwd=src, check=True, capture_output=True)

    monkeypatch.setattr(tosca.loader, "FORCE_SAFE_MODE", "1")
    pull(repo, "main")

    assert (
        repo.repo.git.config("--get", "core.symlinks") == "false"
    ), "pull should have persisted the setting"
    escape2 = os.path.join(dest, "escape2.txt")
    assert os.path.exists(escape2), "the pull should have brought the new file"
    assert not os.path.islink(escape2)
    assert open(escape2).read() == "/etc/hosts"


# ---------------------------------------------------------------------------
# YamlConfig.load_yaml confinement
# ---------------------------------------------------------------------------


def _project_with_include(tmp_path, include: str):
    """A project whose unfurl.yaml pulls in `include`, plus a file outside it."""
    project = tmp_path / "project"
    project.mkdir()
    # schema-valid, so the unrestricted half of the chain test gets past
    # unfurl-schema.json and actually proves the include resolved
    (tmp_path / "escape.yaml").write_text("environments:\n  stolen: {}\n")
    (project / "inside.yaml").write_text("ok: true\n")
    (project / "unfurl.yaml").write_text(
        f"apiVersion: unfurl/v1alpha1\nkind: Project\n+include: {include}\n"
    )
    return project


def test_load_yaml_refuses_path_outside_base_dir_in_safe_mode(tmp_path):
    from unfurl.yamlloader import YamlConfig
    from unfurl.util import UnfurlError

    project = _project_with_include(tmp_path, "inside.yaml")
    config = YamlConfig(path=str(project / "unfurl.yaml"), validate=False, safe_mode=True)

    with pytest.raises(UnfurlError, match="safe mode"):
        config.load_yaml("../escape.yaml")
    # the same document inside the project still loads
    assert config.load_yaml("inside.yaml")[1] == {"ok": True}


def test_load_yaml_allows_escape_when_not_safe_mode(tmp_path):
    """Before-state: the path really is reachable without the flag."""
    from unfurl.yamlloader import YamlConfig

    project = _project_with_include(tmp_path, "inside.yaml")
    config = YamlConfig(path=str(project / "unfurl.yaml"), validate=False)

    assert config.load_yaml("../escape.yaml")[1] == {"environments": {"stolen": {}}}


def test_load_yaml_refuses_remote_document_in_safe_mode(tmp_path, monkeypatch):
    from unfurl import yamlloader
    from unfurl.yamlloader import YamlConfig
    from unfurl.util import UnfurlError

    def _boom(*a, **kw):
        raise AssertionError("should not have reached the network")

    monkeypatch.setattr(yamlloader, "urlopen", _boom)
    project = _project_with_include(tmp_path, "inside.yaml")
    config = YamlConfig(path=str(project / "unfurl.yaml"), validate=False, safe_mode=True)

    with pytest.raises(UnfurlError, match="safe mode"):
        config.load_yaml("https://example.com/evil.yaml")


def test_clone_keeps_safe_mode(tmp_path):
    """A reload must not drop the restriction."""
    from unfurl.yamlloader import YamlConfig
    from unfurl.util import UnfurlError

    project = _project_with_include(tmp_path, "inside.yaml")
    config = YamlConfig(
        path=str(project / "unfurl.yaml"), validate=False, safe_mode=True
    ).clone(validate=False)
    with pytest.raises(UnfurlError, match="safe mode"):
        config.load_yaml("../escape.yaml")


# ---------------------------------------------------------------------------
# The chain: LocalEnv override -> Project -> LocalConfig -> YamlConfig
# ---------------------------------------------------------------------------


def test_safe_mode_override_reaches_yamlconfig(tmp_path):
    """An include reaching out of the project fails when the override is set.

    Unit tests on YamlConfig alone don't prove the override is plumbed
    through Project and LocalConfig; this is what does.
    """
    from unfurl.localenv import LocalEnv
    from unfurl.util import UnfurlError

    project = _project_with_include(tmp_path, "../escape.yaml")

    # without the override the include resolves
    unrestricted = LocalEnv(str(project / "unfurl.yaml"), can_be_empty=True)
    assert unrestricted.project
    assert "stolen" in (
        unrestricted.project.localConfig.config.expanded.get("environments") or {}
    ), "before-state: the include resolves without the override"

    with pytest.raises(UnfurlError):
        LocalEnv(
            str(project / "unfurl.yaml"),
            can_be_empty=True,
            overrides=dict(safe_mode=True),
        )


def test_local_repositories_outside_project_ignored_in_safe_mode(tmp_path):
    """`_set_repos` must not register a repository outside the project."""
    from unfurl.localenv import LocalEnv

    outside = _repo_with_symlink(tmp_path / "outside_repo", target="real.txt")
    project = tmp_path / "project"
    project.mkdir()
    (project / "unfurl.yaml").write_text(
        "apiVersion: unfurl/v1alpha1\n"
        "kind: Project\n"
        "localRepositories:\n"
        f"  {outside}:\n"
        "    url: https://example.com/outside.git\n"
    )

    unrestricted = LocalEnv(str(project / "unfurl.yaml"), can_be_empty=True)
    assert unrestricted.project
    assert outside in unrestricted.project.workingDirs, "before-state: it registers"

    restricted = LocalEnv(
        str(project / "unfurl.yaml"), can_be_empty=True, overrides=dict(safe_mode=True)
    )
    assert restricted.project
    assert outside not in restricted.project.workingDirs


def test_make_readonly_localenv_sets_the_override(tmp_path):
    """The server is what turns this on; assert it actually does.

    Without this, every test above passes while the server still loads
    projects unrestricted -- the guards would be dead code in production.
    """
    from unfurl.server.serve import app, _make_readonly_localenv

    project = tmp_path / "project"
    project.mkdir()
    (project / "unfurl.yaml").write_text("apiVersion: unfurl/v1alpha1\nkind: Project\n")

    with app.app_context():
        app.config["UNFURL_OPTIONS"] = {}
        app.config.pop("UNFURL_GUI_MODE", None)
        app.config.pop("UNFURL_CURRENT_WORKING_DIR", None)
        err, local_env = _make_readonly_localenv(str(project), "")

    assert err is None, err
    assert local_env is not None
    assert local_env.overrides.get("safe_mode") is True
    assert local_env.project and local_env.project.safe_mode is True


# ---------------------------------------------------------------------------
# UNFURL_CLOUD_VARS_URL is exempt from the safe-mode remote-document refusal
# ---------------------------------------------------------------------------


def _project_including(tmp_path, include_url: str):
    """A project whose unfurl.yaml pulls its variables from `include_url`."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "unfurl.yaml").write_text(
        "apiVersion: unfurl/v1alpha1\n"
        "kind: Project\n"
        "environments:\n"
        "  defaults:\n"
        "    variables:\n"
        "      +?include-env:\n"
        f"        file: {include_url}\n"
        "        merge: maplist\n"
    )
    return project


def _serve_json(body: str):
    """A local http server returning `body`; returns (url, shutdown)."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            payload = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_port}/vars.json", httpd.shutdown


def test_cloud_vars_url_loads_in_safe_mode(tmp_path):
    """The server sets this override itself, so it is trusted."""
    from unfurl.localenv import LocalEnv

    # gitlab's list-project-variables reply, which is what `maplist` expects
    url, shutdown = _serve_json(
        '[{"variable_type": "env_var", "key": "CLOUD_VAR",'
        ' "value": "from_the_server", "environment_scope": "*"}]'
    )
    try:
        project = _project_including(tmp_path, "${UNFURL_CLOUD_VARS_URL}")
        local_env = LocalEnv(
            str(project / "unfurl.yaml"),
            can_be_empty=True,
            overrides=dict(
                safe_mode=True, ENVIRONMENT="defaults", UNFURL_CLOUD_VARS_URL=url
            ),
        )
        assert local_env.project and local_env.project.safe_mode is True
        variables = local_env.get_context().get("variables") or {}
        assert variables.get("CLOUD_VAR") == "from_the_server", variables
    finally:
        shutdown()


def test_other_remote_includes_still_blocked_in_safe_mode(tmp_path):
    """The exemption is that one url, not remote includes in general.

    Without this the fix would read as "safe mode allows http" -- the
    project names this url itself, so it must stay refused.
    """
    from unfurl.localenv import LocalEnv
    from unfurl.util import UnfurlError

    url, shutdown = _serve_json(
        '[{"key": "SNEAKY", "value": "yes", "environment_scope": "*"}]'
    )
    try:
        project = _project_including(tmp_path, url)
        with pytest.raises(UnfurlError):
            LocalEnv(
                str(project / "unfurl.yaml"),
                can_be_empty=True,
                overrides=dict(safe_mode=True, ENVIRONMENT="defaults"),
            )
    finally:
        shutdown()


# ---------------------------------------------------------------------------
# cloud_vars_url is whitelisted before the server will fetch it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,accepted",
    [
        # what unfurl-gui's unfurl_cloud_vars_url() builds
        (
            "https://unfurl.cloud/api/v4/projects/42/variables"
            "?per_page=1000&private_token=SECRET",
            True,
        ),
        ("https://unfurl.cloud/api/v4/projects/a%2Fb/variables", True),
        # a different host would be handed the token
        (
            "https://evil.test/api/v4/projects/42/variables?private_token=SECRET",
            False,
        ),
        # same host, but any other endpoint is an SSRF into the server's network
        ("https://unfurl.cloud/api/v4/projects/42/repository/archive", False),
        ("https://unfurl.cloud/", False),
        # scheme games
        ("file:///etc/passwd", False),
        ("http://unfurl.cloud.evil.test/api/v4/projects/42/variables", False),
        # a port mismatch is a different service on the same host
        ("https://unfurl.cloud:8443/api/v4/projects/42/variables", False),
    ],
)
def test_cloud_vars_url_whitelist(url, accepted):
    from unfurl.server import endpoints
    from unfurl.server.serve import app

    with app.app_context():
        app.config["UNFURL_CLOUD_SERVER"] = "https://unfurl.cloud"
        rejected = endpoints._rejected_cloud_vars_url(url)
    # a reason means refused; "" means it may be fetched
    assert (not rejected) is accepted, rejected
    # the reason must never quote the url -- it carries a private token
    assert url not in rejected


def test_cloud_vars_url_rejected_when_cloud_server_is_a_local_path():
    """The unit tests configure a filesystem path as the cloud server.

    There is no origin to match then, so nothing can be accepted -- better
    than matching an empty hostname against a url's.
    """
    from unfurl.server import endpoints
    from unfurl.server.serve import app

    with app.app_context():
        app.config["UNFURL_CLOUD_SERVER"] = "/tmp/somewhere/remote.git"
        assert endpoints._rejected_cloud_vars_url(
            "https://unfurl.cloud/api/v4/projects/42/variables"
        )
