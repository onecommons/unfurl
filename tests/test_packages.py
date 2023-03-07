import os
import traceback
from click.testing import CliRunner
from unfurl.__main__ import cli
import git
from unfurl.localenv import LocalEnv
from unfurl.packages import Package, get_package_id_from_url


def test_package_rules():
    # test:
    # * following multiple UNFURL_PACKAGE_RULES
    # * resolving remote tags work
    package_id, revision, url = get_package_id_from_url("unfurl.cloud/onecommons/*")
    assert package_id, (package_id, revision, url)
    assert not url, (package_id, revision, url)
    types_url = "https://unfurl.cloud/onecommons/unfurl-types.git"
    latest_version_tag = Package("", types_url, None).find_latest_semver_from_repo()
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
                            url="https://gitlab.com/onecommons/unfurl-types.git#v0.1.0"
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
