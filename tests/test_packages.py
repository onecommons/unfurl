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
    get_package_id_from_url,
    get_package_from_url,
    reverse_rules_for_canonical,
    find_canonical,
)
from unfurl.repo import get_remote_tags
from unfurl.util import UnfurlError, taketwo
from unfurl.yamlmanifest import YamlManifest


def _build_package_specs(env_package_spec):
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
    types_url = "https://unfurl.cloud/onecommons/unfurl-types.git"
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
                        "gitlab.com/onecommons/unfurl-types": dict(
                            url="https://gitlab.com/onecommons/unfurl-types.git#v0.7.5"
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

            clonedtypes = git.cmd.Git("application-blueprint/unfurl-types")
            origin = clonedtypes.remote(["get-url", "origin"])
            # should be on unfurl.cloud:
            assert origin == types_url, origin
            # XXX enable this assert when unfurl-types have v1 tag
            # assert clonedtypes.describe() == "v"+latest_version_tag

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
ENSEMBLE_UNFURL_PAST = _ENSEMBLE_TPL % "v0.9.1"
ENSEMBLE_UNFURL_FUTURE = _ENSEMBLE_TPL % "2.0"


def test_unfurl_dependencies():
    assert YamlManifest(ENSEMBLE_UNFURL_PAST)
    with pytest.raises(UnfurlError) as err:
        YamlManifest(ENSEMBLE_UNFURL_FUTURE)
        assert (
            "github.com/onecommons/unfurl has version 2.0 but incompatible version"
            in str(err)
        )


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
