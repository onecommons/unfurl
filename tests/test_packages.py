import json
import os
import traceback
from click.testing import CliRunner
from unfurl.__main__ import cli
import git
import pytest
from unfurl.localenv import LocalEnv
from unfurl.packages import (
    PackageSpec,
    Package,
    ProxiedRepo,
    get_package_id_from_url,
    get_package_from_url,
    reverse_rules_for_canonical,
    find_canonical,
)
from unfurl.repo import get_remote_tags
from unfurl.util import UnfurlError, taketwo
from unfurl.yamlmanifest import YamlManifest
from unfurl.yamlloader import get_tags_from_proxy


def _build_package_specs(env_package_spec):
    if isinstance(env_package_spec, PackageSpec):
        return [env_package_spec]
    package_specs = []
    for key, value in taketwo(env_package_spec.split()):
        package_specs.append(PackageSpec(key, value, None))
    return package_specs


def _apply_package_rules(test_url, env_package_spec):
    package_specs = _build_package_specs(env_package_spec)
    package = get_package_from_url(test_url)
    assert package
    changed = PackageSpec.update_package(package_specs, package)
    return package, package_specs


def test_package_rules():
    test_url = "https://unfurl.cloud/onecommons/blueprints/wordpress"
    env_package_spec = "gitlab.com/onecommons/* unfurl.cloud/onecommons/* unfurl.cloud/onecommons/* http://tunnel.abreidenbach.com:3000/onecommons/*#main"
    package, package_specs = _apply_package_rules(test_url, env_package_spec)
    assert (
        package.url
        == "http://tunnel.abreidenbach.com:3000/onecommons/blueprints/wordpress#main"
    )
    assert env_package_spec == " ".join([p.as_env_value() for p in package_specs])

    package, package_specs = _apply_package_rules(
        "https://gitlab.com/onecommons/unfurl-types", env_package_spec
    )
    assert (
        package.url
        == "http://tunnel.abreidenbach.com:3000/onecommons/unfurl-types#main"
    )

    env_package_spec = """unfurl.cloud/onecommons/* staging.unfurl.cloud/onecommons/*
    gitlab.com/onecommons/* staging.unfurl.cloud/onecommons/*
    app.dev.unfurl.cloud/* staging.unfurl.cloud/*
    staging.unfurl.cloud/onecommons/* #main"""

    package, package_specs = _apply_package_rules(
        "https://app.dev.unfurl.cloud/onecommons/unfurl-types", env_package_spec
    )
    assert (
        package.url == "https://staging.unfurl.cloud/onecommons/unfurl-types.git#main:"
    )
    assert " ".join(env_package_spec.split()) == " ".join([
        p.as_env_value() for p in package_specs
    ])

    package, package_specs = _apply_package_rules(
        "https://app.dev.unfurl.cloud/onecommons/unfurl-types.git", env_package_spec
    )
    assert (
        package.url == "https://staging.unfurl.cloud/onecommons/unfurl-types.git#main:"
    )

    package, package_specs = _apply_package_rules(
        "https://staging.unfurl.cloud/onecommons/unfurl-types", env_package_spec
    )
    assert package.url == "https://staging.unfurl.cloud/onecommons/unfurl-types#main"

    package, package_specs = _apply_package_rules(
        "https://app.dev.unfurl.cloud/user/dashboard", env_package_spec
    )
    assert package.url == "https://staging.unfurl.cloud/user/dashboard.git"

    with pytest.raises(UnfurlError) as err:
        _apply_package_rules(
            "https://app.dev.unfurl.cloud/user/dashboard",
            "http://tunnel.abreidenbach.com:3000/onecommons/* #main",
        )
    assert "Malformed package spec" in str(err)

    unfurl_spec = PackageSpec(
        "github.com/onecommons/unfurl", "file:///home/unfurl/unfurl", None
    )
    unfurl_package = Package(
        "github.com/onecommons/unfurl", "https://github.com/onecommons/unfurl", "v0.9.1"
    )
    assert unfurl_package.url
    changed = PackageSpec.update_package([unfurl_spec], unfurl_package)
    assert changed
    assert unfurl_package.url == "file:///home/unfurl/unfurl"

    # no wildcard in rule setting revision
    env_package_spec = """unfurl.cloud/onecommons/* staging.unfurl.cloud/onecommons/*
    gitlab.com/onecommons/* staging.unfurl.cloud/onecommons/*
    app.dev.unfurl.cloud/* staging.unfurl.cloud/*
    staging.unfurl.cloud/onecommons/unfurl-types #main"""

    package, package_specs = _apply_package_rules(
        "https://staging.unfurl.cloud/onecommons/unfurl-types", env_package_spec
    )
    assert package.url == "https://staging.unfurl.cloud/onecommons/unfurl-types#main"
    assert " ".join(env_package_spec.split()) == " ".join([
        p.as_env_value() for p in package_specs
    ])

    package, package_specs = _apply_package_rules(
        "staging.unfurl.cloud/onecommons/unfurl-types", env_package_spec
    )
    assert (
        package.url == "https://staging.unfurl.cloud/onecommons/unfurl-types.git#main"
    )

    for head_package_spec in [
        PackageSpec(
            "unfurl.cloud/onecommons/std", "https://gitlab.com/onecommons/std", "HEAD"
        ),
        "unfurl.cloud/onecommons/std https://gitlab.com/onecommons/std#HEAD",
    ]:
        package, package_specs = _apply_package_rules(
            "unfurl.cloud/onecommons/std", head_package_spec
        )
        assert package.url == "https://gitlab.com/onecommons/std"
        assert not package.revision
        assert package.locked

    package, package_specs = _apply_package_rules(
        "unfurl.cloud/onecommons/std", "unfurl.cloud/onecommons/std #HEAD"
    )
    assert package.url == "https://unfurl.cloud/onecommons/std.git"
    assert not package.revision
    assert package.locked

    package, package_specs = _apply_package_rules(
        "https://staging.unfurl.cloud/onecommons/unfurl-types#:v2", env_package_spec
    )
    # note: v2 is different package to #main rule isn't applied
    assert package.url == "https://staging.unfurl.cloud/onecommons/unfurl-types#:v2"


def test_find_canonical():
    rules = "gitlab.com/onecommons/* staging.unfurl.cloud/onecommons/* unfurl.cloud/onecommons/* staging.unfurl.cloud/onecommons/*"
    specs = _build_package_specs(rules)
    assert len(specs) == 2
    canonical = "unfurl.cloud"
    assert reverse_rules_for_canonical(specs, canonical) == [
        PackageSpec("staging.unfurl.cloud/onecommons/*", "unfurl.cloud/onecommons/*")
    ]
    namespaces = [
        "staging.unfurl.cloud/onecommons/std:dns_services",
        "unfurl.cloud/onecommons/std",
        "gitlab.com/onecommons/blueprints/wordpress",
        "gitlab.com/some-user",
    ]
    expected = [
        "unfurl.cloud/onecommons/std:dns_services",
        "unfurl.cloud/onecommons/std",
        "unfurl.cloud/onecommons/blueprints/wordpress",
        "gitlab.com/some-user",
    ]
    for namespace_id, expected in zip(namespaces, expected):
        assert expected == find_canonical(specs, canonical, namespace_id)


def test_remote_tags():
    # test:
    # * following multiple UNFURL_PACKAGE_RULES
    # * resolving remote tags work
    package_id, url, revision = get_package_id_from_url("unfurl.cloud/onecommons/*")
    assert package_id, (package_id, revision, url)
    assert not url, (package_id, revision, url)
    types_url = "https://unfurl.cloud/onecommons/std"
    latest_version_tag = Package("", types_url, None).find_latest_semver_from_repo(
        get_remote_tags
    )
    assert latest_version_tag
    # replace gitlab.com/onecommons packages unfurl.cloud/onecommons packages
    package_rules_envvar = "gitlab.com/onecommons/* unfurl.cloud/onecommons/*"
    runner = CliRunner()
    try:
        UNFURL_PACKAGE_RULES = os.environ.get("UNFURL_PACKAGE_RULES")
        with runner.isolated_filesystem():
            os.environ["UNFURL_PACKAGE_RULES"] = package_rules_envvar
            result = runner.invoke(
                cli,
                [
                    "--home",
                    "",
                    "clone",
                    "--mono",
                    "--skeleton=dashboard",
                    "--use-deployment-blueprint=testing",
                    "https://gitlab.com/onecommons/project-templates/application-blueprint.git",
                ],
            )
            # uncomment this to see output:
            # print("result.output", result.exit_code, result.output)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )

            localConfig = LocalEnv("application-blueprint").project.localConfig
            localConfig.config.config["environments"] = dict(
                defaults={
                    "repositories": {
                        "gitlab.com/onecommons/std": dict(
                            url=f"https://gitlab.com/onecommons/std.git#v{latest_version_tag}"
                        )
                    }
                }
            )
            localConfig.config.save()

            result = runner.invoke(
                cli, ["--home", "", "plan", "--starttime=1", "application-blueprint"]
            )

            # print("result.output", result.exit_code, result.output)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            assert result.exit_code == 0, result

            clonedtypes = git.cmd.Git("application-blueprint/std")
            origin = clonedtypes.remote(["get-url", "origin"])
            # should be on unfurl.cloud:
            assert origin == types_url, origin
            assert clonedtypes.describe() == "v" + latest_version_tag

            # add a rule to set the version to the "main" branch
            os.environ["UNFURL_PACKAGE_RULES"] = (
                package_rules_envvar + " unfurl.cloud/onecommons/* #main"
            )
            result = runner.invoke(
                cli, ["--home", "", "plan", "--starttime=1", "application-blueprint"]
            )

            # print("result.output", result.exit_code, result.output)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            assert result.exit_code == 0, result
            tip = clonedtypes.describe(always=True)
            # this revision is more recent than the released version
            assert latest_version_tag != tip, tip

    finally:
        if UNFURL_PACKAGE_RULES:
            os.environ["UNFURL_PACKAGE_RULES"] = UNFURL_PACKAGE_RULES


def test_get_tags_from_proxy():
    tags = get_tags_from_proxy("unfurl.cloud/onecommons/blueprints/baserow")
    assert tags == ["v1.0.0", "v0.1.0"]
    # make missing packages return None instead of empty list:
    tags = get_tags_from_proxy("unfurl.cloud/onecommons/blueprints/nonexistent")
    assert tags is None
    tags = get_tags_from_proxy("https://gitlab.com/onecommons/std.git")
    assert tags[-1] == "v1.0.0", tags


_ENSEMBLE_TPL = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
spec:
  service_template:
    repositories:
      unfurl:
        url: https://github.com/onecommons/unfurl
        revision: %s
    imports:
      - repository: unfurl
        file: configurators/templates/dns.yaml
"""
ENSEMBLE_UNFURL_UNRELEASED = _ENSEMBLE_TPL % "v0.9.1"
ENSEMBLE_UNFURL_PAST = _ENSEMBLE_TPL % "v1.0"
ENSEMBLE_UNFURL_FUTURE = _ENSEMBLE_TPL % "2.0"

def test_unfurl_dependencies():
    assert YamlManifest(ENSEMBLE_UNFURL_UNRELEASED) # warns
    assert YamlManifest(ENSEMBLE_UNFURL_PAST) # ok
    with pytest.raises(UnfurlError) as err:
        YamlManifest(ENSEMBLE_UNFURL_FUTURE)  # fails
    assert "was specified but the running version is" in str(err)


_SAMPLE_INFO = {
    "Version": "v1.0.0",
    "Time": "2024-01-12T16:15:59Z",
    "Origin": {
        "VCS": "git",
        "URL": "https://unfurl.cloud/onecommons/blueprints/baserow.git",
        "Hash": "f3d902ae0ec52d4f869f8dbeb0c6438eeeb83030",
        "Ref": "refs/tags/v1.0.0",
    },
}


@pytest.fixture
def proxied_repo(tmp_path) -> ProxiedRepo:
    """Create a ProxiedRepo with sample metadata, a dummy file, and files.json."""
    import zlib

    proxied_dir = tmp_path / ".proxied"
    proxied_dir.mkdir()
    (proxied_dir / "info.json").write_text(json.dumps(_SAMPLE_INFO))
    content = b"hello"
    (tmp_path / "dummy.txt").write_bytes(content)
    crc = zlib.crc32(content) & 0xFFFFFFFF
    (proxied_dir / "files.json").write_text(json.dumps({"dummy.txt": crc}))
    return ProxiedRepo(str(tmp_path))


def test_proxied_repo_properties(proxied_repo: ProxiedRepo):
    assert os.path.isdir(proxied_repo.working_dir)
    assert proxied_repo.revision == "f3d902ae0ec52d4f869f8dbeb0c6438eeeb83030"
    assert proxied_repo.current_tag == "v1.0.0"
    assert proxied_repo.url == "https://unfurl.cloud/onecommons/blueprints/baserow.git"


def test_proxied_repo_resolve_rev_spec(proxied_repo: ProxiedRepo):
    full_hash = "f3d902ae0ec52d4f869f8dbeb0c6438eeeb83030"
    # Exact hash
    assert proxied_repo.resolve_rev_spec(full_hash) == full_hash
    # Tag
    assert proxied_repo.resolve_rev_spec("v1.0.0") == full_hash
    # Hash prefix (7+ chars)
    assert proxied_repo.resolve_rev_spec("f3d902a") == full_hash
    # Non-match
    assert proxied_repo.resolve_rev_spec("v2.0.0") is None
    assert proxied_repo.resolve_rev_spec("abc") is None


def test_proxied_repo_find_remote_url(proxied_repo: ProxiedRepo):
    origin = "https://unfurl.cloud/onecommons/blueprints/baserow.git"
    # URL match
    assert proxied_repo.find_remote_url(url=origin) == origin
    # URL mismatch
    assert proxied_repo.find_remote_url(url="github.com") is None
    # Host match
    assert proxied_repo.find_remote_url(host="unfurl.cloud") == origin
    # Host mismatch
    assert proxied_repo.find_remote_url(host="github.com") is None


def test_proxied_repo_clone(proxied_repo: ProxiedRepo, tmp_path):
    clone_path = str(tmp_path / "clone")
    cloned = proxied_repo.clone(clone_path)
    assert isinstance(cloned, ProxiedRepo)
    assert cloned.working_dir == clone_path
    assert cloned.revision == proxied_repo.revision
    assert cloned.current_tag == proxied_repo.current_tag
    assert os.path.exists(os.path.join(clone_path, "dummy.txt"))


def test_proxied_repo_is_dirty_clean(proxied_repo: ProxiedRepo):
    """Unmodified repo is not dirty."""
    assert not proxied_repo.is_dirty()
    assert not proxied_repo.is_dirty(untracked_files=True)


def test_proxied_repo_is_dirty_modified(proxied_repo: ProxiedRepo):
    """Modifying a tracked file makes the repo dirty."""
    dummy = os.path.join(proxied_repo.working_dir, "dummy.txt")
    with open(dummy, "w") as f:
        f.write("modified content")
    assert proxied_repo.is_dirty()


def test_proxied_repo_is_dirty_deleted(proxied_repo: ProxiedRepo):
    """Deleting a tracked file makes the repo dirty."""
    os.remove(os.path.join(proxied_repo.working_dir, "dummy.txt"))
    assert proxied_repo.is_dirty()


def test_proxied_repo_is_dirty_untracked(proxied_repo: ProxiedRepo):
    """Untracked file only counts when untracked_files=True."""
    new_file = os.path.join(proxied_repo.working_dir, "new_file.txt")
    with open(new_file, "w") as f:
        f.write("I'm new")
    # Without untracked_files, still clean (tracked files unchanged).
    assert not proxied_repo.is_dirty(untracked_files=False)
    # With untracked_files, now dirty.
    assert proxied_repo.is_dirty(untracked_files=True)


def test_proxied_repo_is_dirty_gitignore(proxied_repo: ProxiedRepo):
    """Files matching .gitignore patterns are not considered untracked."""
    gitignore = os.path.join(proxied_repo.working_dir, ".gitignore")
    with open(gitignore, "w") as f:
        f.write("*.log\nbuild/\n")
    # Create files matching the ignore patterns.
    with open(os.path.join(proxied_repo.working_dir, "debug.log"), "w") as f:
        f.write("log data")
    build_dir = os.path.join(proxied_repo.working_dir, "build")
    os.makedirs(build_dir)
    with open(os.path.join(build_dir, "output.o"), "w") as f:
        f.write("binary")
    # .gitignore itself is untracked, so we expect dirty.
    assert proxied_repo.is_dirty(untracked_files=True)
    # But if we only check ignored files (remove .gitignore from untracked),
    # add .gitignore to the ignore list too.
    with open(gitignore, "w") as f:
        f.write("*.log\nbuild/\n.gitignore\n")
    assert not proxied_repo.is_dirty(untracked_files=True)


def test_proxied_repo_is_dirty_path_filter(proxied_repo: ProxiedRepo):
    """The path parameter restricts dirty checks to a subdirectory."""
    subdir = os.path.join(proxied_repo.working_dir, "sub")
    os.makedirs(subdir)
    with open(os.path.join(subdir, "extra.txt"), "w") as f:
        f.write("extra")
    # Untracked file is in "sub/", not in root "dummy.txt" area.
    assert not proxied_repo.is_dirty(untracked_files=True, path=os.path.join(proxied_repo.working_dir, "dummy"))
    assert proxied_repo.is_dirty(untracked_files=True, path=subdir)


def test_proxied_repo_is_dirty_no_files_json(tmp_path):
    """When files.json doesn't exist, is_dirty returns False."""
    proxied_dir = tmp_path / ".proxied"
    proxied_dir.mkdir()
    (proxied_dir / "info.json").write_text(json.dumps(_SAMPLE_INFO))
    (tmp_path / "dummy.txt").write_text("hello")
    repo = ProxiedRepo(str(tmp_path))
    assert not repo.is_dirty()
    assert not repo.is_dirty(untracked_files=True)


def test_proxied_repo_missing_info(tmp_path):
    with pytest.raises(FileNotFoundError):
        ProxiedRepo(str(tmp_path))


def test_get_proxy_url():
    url = ProxiedRepo.get_proxy_url("https://unfurl.cloud/onecommons/blueprints/baserow.git")
    assert url == "https://proxy.golang.org/unfurl.cloud/onecommons/blueprints/baserow/"

    # Custom GOPROXY
    old = os.environ.get("GOPROXY")
    try:
        os.environ["GOPROXY"] = "https://custom.proxy/"
        url = ProxiedRepo.get_proxy_url("https://unfurl.cloud/onecommons/blueprints/baserow.git")
        assert url == "https://custom.proxy/unfurl.cloud/onecommons/blueprints/baserow/"

        # GOPROXY=direct
        os.environ["GOPROXY"] = "direct"
        assert ProxiedRepo.get_proxy_url("https://unfurl.cloud/onecommons/blueprints/baserow.git") is None
    finally:
        if old is None:
            os.environ.pop("GOPROXY", None)
        else:
            os.environ["GOPROXY"] = old

    # Invalid URL (relative path)
    assert ProxiedRepo.get_proxy_url("./local/path") is None


def test_proxied_repo_create_repo(tmp_path):
    """Integration test: download a real package from Go proxy."""
    working_dir = str(tmp_path / "baserow")
    repo = ProxiedRepo.create_repo(
        "https://unfurl.cloud/onecommons/blueprints/baserow.git",
        "v1.0.0",
        working_dir,
    )
    assert repo is not None
    assert isinstance(repo, ProxiedRepo)
    assert repo.url == "https://unfurl.cloud/onecommons/blueprints/baserow.git"
    assert repo.current_tag == "v1.0.0"
    assert repo.revision  # should be a git hash
    # Verify files were extracted
    assert os.path.exists(os.path.join(working_dir, ".proxied", "info.json"))
    # Verify files.json was created with CRC-32 checksums
    files_json_path = os.path.join(working_dir, ".proxied", "files.json")
    assert os.path.exists(files_json_path)
    with open(files_json_path) as f:
        file_checksums = json.load(f)
    assert len(file_checksums) > 0, "files.json should list extracted files"
    # Every value should be a non-negative integer (CRC-32)
    for rel_path, crc in file_checksums.items():
        assert isinstance(crc, int) and crc >= 0, f"Bad CRC for {rel_path}: {crc}"
        assert os.path.exists(os.path.join(working_dir, rel_path)), (
            f"files.json references missing file: {rel_path}"
        )
    # The baserow blueprint should have at least one file
    extracted = [
        f for f in os.listdir(working_dir) if f != ".proxied"
    ]
    assert len(extracted) > 0, "Expected extracted files from zip"
    # Freshly created repo should not be dirty
    assert not repo.is_dirty()
    assert not repo.is_dirty(untracked_files=True)


def test_proxied_repo_create_repo_failure(tmp_path):
    """Nonexistent package should return None."""
    working_dir = str(tmp_path / "nonexistent")
    repo = ProxiedRepo.create_repo(
        "https://unfurl.cloud/onecommons/blueprints/this-does-not-exist.git",
        "v99.99.99",
        working_dir,
    )
    assert repo is None


def test_proxied_repo_convert_to_git(tmp_path):
    """Integration test: create a proxied repo then convert it to a real git repo."""
    working_dir = str(tmp_path / "baserow")
    proxied = ProxiedRepo.create_repo(
        "https://unfurl.cloud/onecommons/blueprints/baserow.git",
        "v1.0.0",
        working_dir,
    )
    assert proxied is not None
    original_revision = proxied.revision
    original_files = sorted(f for f in os.listdir(working_dir) if f != ".proxied")

    git_repo = proxied.convert_to_git()

    # .proxied directory should be gone
    assert not os.path.exists(os.path.join(working_dir, ".proxied"))
    # .git directory should exist
    assert os.path.isdir(os.path.join(working_dir, ".git"))
    # Original files should still be present
    current_files = sorted(f for f in os.listdir(working_dir) if f not in (".git",))
    assert current_files == original_files
    # RepoView should have the correct url and revision
    assert "unfurl.cloud" in git_repo.url
    assert git_repo.revision == original_revision
    # Working tree should be clean (no modifications)
    assert not git_repo.repo.is_dirty()


@pytest.fixture
def proxied_repo_with_origin(tmp_path) -> ProxiedRepo:
    """Create a ProxiedRepo whose Origin.URL points to a real local git repo.

    This allows convert_to_git() / _ensure_git() to successfully clone.
    """
    import zlib

    # 1. Create a local bare git repo as the "origin"
    origin_path = str(tmp_path / "origin.git")
    origin = git.Repo.init(origin_path, bare=True)

    # Create a temporary working copy, add a file, commit, tag, push
    work = str(tmp_path / "work")
    work_repo = git.Repo.init(work)
    dummy = os.path.join(work, "dummy.txt")
    with open(dummy, "w") as f:
        f.write("hello")
    work_repo.index.add(["dummy.txt"])
    commit = work_repo.index.commit("initial")
    work_repo.create_remote("origin", origin_path)
    work_repo.remotes.origin.push("HEAD:refs/heads/main")
    work_repo.create_tag("v1.0.0")
    work_repo.remotes.origin.push("v1.0.0")

    # 2. Create the proxied repo directory
    repo_dir = tmp_path / "proxied"
    repo_dir.mkdir()
    proxied_dir = repo_dir / ".proxied"
    proxied_dir.mkdir()

    info = {
        "Version": "v1.0.0",
        "Time": "2024-01-12T16:15:59Z",
        "Origin": {
            "VCS": "git",
            "URL": origin_path,
            "Hash": commit.hexsha,
            "Ref": "refs/tags/v1.0.0",
        },
    }
    (proxied_dir / "info.json").write_text(json.dumps(info))
    content = b"hello"
    (repo_dir / "dummy.txt").write_bytes(content)
    crc = zlib.crc32(content) & 0xFFFFFFFF
    (proxied_dir / "files.json").write_text(json.dumps({"dummy.txt": crc}))

    return ProxiedRepo(str(repo_dir))


def test_proxied_repo_commit_no_changes(proxied_repo_with_origin: ProxiedRepo):
    """Calling commit() with no staged changes converts to git and creates a commit."""
    repo = proxied_repo_with_origin
    wd = repo.working_dir
    # Before commit: .proxied should exist, .git should not
    assert os.path.isdir(os.path.join(wd, ".proxied"))
    assert not os.path.isdir(os.path.join(wd, ".git"))

    # this commit is a no-op since nothing changed
    repo.commit("no op commit")

    # After commit: converted to git, .proxied removed
    assert not os.path.isdir(os.path.join(wd, ".proxied"))
    assert os.path.isdir(os.path.join(wd, ".git"))
    # The commit message should appear in the log
    git_repo = git.Repo(wd)
    assert git_repo.head.commit.message == "initial", str(
        git_repo.head.commit.diff(None, create_patch=True)
    )


def test_proxied_repo_add_all_and_commit(proxied_repo_with_origin, tmp_path):
    """add_all() stages new files, commit() creates a commit with them."""
    repo = proxied_repo_with_origin
    wd = repo.working_dir
    # Add a new file before committing
    new_file = os.path.join(wd, "new_file.txt")
    with open(new_file, "w") as f:
        f.write("new content")

    repo.as_repo_view().add_all()
    repo.commit("add new file")

    git_repo = git.Repo(wd)
    assert "new_file.txt" in [
        item.path for item in git_repo.head.commit.tree.traverse()
    ]
    assert git_repo.head.commit.message == "add new file", (
        git_repo.head.commit.stats.files
    )


def test_proxied_repo_add_files_and_commit(proxied_repo_with_origin):
    """add_files() stages specific files, commit() creates a commit."""
    repo = proxied_repo_with_origin
    wd = repo.working_dir
    # Create two files but only stage one
    file_a = os.path.join(wd, "a.txt")
    file_b = os.path.join(wd, "b.txt")
    with open(file_a, "w") as f:
        f.write("aaa")
    with open(file_b, "w") as f:
        f.write("bbb")

    repo.add_relative_path("a.txt")
    repo.commit("add a only")

    git_repo = git.Repo(wd)
    committed_paths = [item.path for item in git_repo.head.commit.tree.traverse()]
    assert "a.txt" in committed_paths
    # b.txt was not staged so should not be committed
    assert "b.txt" not in committed_paths


def test_proxied_repo_clone_after_convert(proxied_repo_with_origin, tmp_path):
    """After convert_to_git, clone() delegates to GitRepo.clone()."""
    repo = proxied_repo_with_origin
    # Convert to git first
    repo._ensure_git()
    assert repo._git_repo is not None

    clone_path = str(tmp_path / "clone")
    cloned = repo.clone(clone_path)
    # Should be a GitRepo clone, not a ProxiedRepo copytree
    from unfurl.repo import GitRepo

    assert isinstance(cloned, GitRepo)
    assert os.path.isdir(os.path.join(clone_path, ".git"))


def test_proxied_repo_is_dirty_after_convert(proxied_repo_with_origin):
    """After convert_to_git, is_dirty() delegates to GitRepo."""
    repo = proxied_repo_with_origin
    wd = repo.working_dir
    # Convert to git
    repo._ensure_git()

    # Clean state
    assert not repo.is_dirty()

    # Modify a file — should be dirty via git
    with open(os.path.join(wd, "dummy.txt"), "w") as f:
        f.write("changed")
    assert repo.is_dirty()


if __name__ == "__main__":
    import sys

    test_url = sys.argv[1]
    package = get_package_from_url(test_url)
    assert package
    print("initial package", package.package_id, package.url, package.revision)
    if len(sys.argv) > 2:
        env_package_spec = sys.argv[2]
        package, package_specs = _apply_package_rules(test_url, env_package_spec)
        print("applying specs", package_specs)
        print(f"converted {test_url} to {package.url}")
