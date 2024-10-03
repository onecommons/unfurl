import os
import os.path
import threading
import unittest
import traceback
from functools import partial
import urllib.request
from click.testing import CliRunner
from unfurl.__main__ import cli


def test_clone(caplog):
    server_address = ("", 8011)
    directory = os.path.join(os.path.dirname(__file__), "fixtures")
    try:
        from http.server import HTTPServer, SimpleHTTPRequestHandler

        handler = partial(SimpleHTTPRequestHandler, directory=directory)
        httpd = HTTPServer(server_address, handler)
    except:  # address might still be in use
        httpd = None
        return

    t = threading.Thread(name="http_thread", target=httpd.serve_forever)
    t.daemon = True
    t.start()

    env_var_url = "http://localhost:8011/envlist.json"
    # make sure this works
    f = urllib.request.urlopen(env_var_url)
    f.close()

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            cli,
            [
                "--home",
                "",
                "clone",
                "https://gitlab.com/onecommons/project-templates/dashboard",
                "--var",
                "UNFURL_CLOUD_VARS_URL",
                env_var_url,
            ],
        )
        # uncomment this to see output:
        # print("result.output", result.exit_code, result.output)
        assert not result.exception, "\n".join(
            traceback.format_exception(*result.exc_info)
        )
        assert result.exit_code == 0, result

        with open("dashboard/local/unfurl.yaml") as f:
            assert env_var_url in f.read()

        result = runner.invoke(
            cli,
            [
                "--home",
                "",
                "status",
                "dashboard",
                "--query",
                "{{ {'get_env': 'UNFURL_VAULT_DEFAULT_PASSWORD'} | eval }}",
            ],
        )
        assert not result.exception, "\n".join(
            traceback.format_exception(*result.exc_info)
        )
        assert result.exit_code == 0, result

        # http://localhost:8011/envlist.json should have been included into the variables section in local/unfurl.yaml
        # so UNFURL_VAULT_DEFAULT_PASSWORD is added to environment variables and vault password should be set to "password"
        assert "Vault password found, configuring vault ids: ['default']" in caplog.text
        assert "password" in result.output.splitlines()[-1]  # cli query result

    if httpd:
        httpd.socket.close()


ensemble_template = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
spec:
  service_template:
    topology_template:
      inputs:
        test1:
          type: string
        test2:
          type: number
"""


def test_inputs():
    # test --var input_X when creating an ensemble and when deploying
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            cli,
            [
                "--home",
                "",
                "init",
                "--var",
                "input_test1",
                "Init",
                "--var",
                "input_test2",
                "1",
            ],
        )
        # uncomment this to see output:
        # print("result.output", result.exit_code, result.output)
        assert not result.exception, "\n".join(
            traceback.format_exception(*result.exc_info)
        )
        assert result.exit_code == 0, result

        with open("ensemble-template.yaml", "w") as f:
            f.write(ensemble_template)

        result = runner.invoke(
            cli,
            [
                "--home",
                "",
                "plan",
                "--var",
                "input_test2",
                "2",
                "--query",
                "Inputs:{{ {'get_input': 'test1'} | eval }},{{ {'get_input': 'test2'} | eval }}",
            ],
        )
        assert not result.exception, "\n".join(
            traceback.format_exception(*result.exc_info)
        )
        assert result.exit_code == 0, result
        assert "Inputs:Init,2" in result.output  # cli query result
