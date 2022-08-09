import json
from multiprocessing import Process
import os
import threading
import time
import unittest
import urllib.request
import requests

from click.testing import CliRunner
from tests.utils import init_project, run_cmd
from unfurl import server


manifest = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
+include:
  file: ensemble-template.yaml
  repository: spec
metadata:
  uri: git-local://67ebee6347690a0b60c045281ab460d067527b6a:/ensemble.yaml
spec:
  service_template:
    repositories:
    # Files that are shared across ensemble instances should be placed in this "spec" repository
      spec:
        url: git-local://0ebc2d5b5204bd6592286dbb28f0c641fc2ac5c2:/.
"""

class TestServer(unittest.TestCase):
    def setUp(self) -> None:
        cli_runner = CliRunner()
        self.runner = cli_runner
        with cli_runner.isolated_filesystem(temp_dir='/tmp/') as tmpdir:
            os.mkdir("ensemble")
            with open("ensemble/ensemble.yaml", "w") as f:
                f.write(manifest)

            # server.serve('localhost', 8081, 'secret', 'ensemble', {})
            p = Process(target=server.serve, args=('localhost', 8081, 'secret', f'{tmpdir}', {}))
            p.start()
            self.server_process = p

            for i in range(5):
                time.sleep(0.2)
                try:
                    url = "http://localhost:8081/health?secret=secret"
                    urllib.request.urlopen(url)
                except Exception as e:
                    pass
                else:
                    return True     

    def test_server_health(self):
        res = requests.get('http://localhost:8081/health', params={'secret': 'secret'})

        assert res.status_code == 200
        assert res.content == b'OK'
    
    def test_server_authentication(self):
        res = requests.get('http://localhost:8081/health')
        assert res.status_code == 401
        assert res.json()["code"] == "UNAUTHORIZED"

        res = requests.get('http://localhost:8081/health', params={'secret': 'secret'})
        assert res.status_code == 200
        assert res.content == b'OK'

        res = requests.get('http://localhost:8081/health', params={'secret': 'wrong'})
        assert res.status_code == 401
        assert res.json()["code"] == "UNAUTHORIZED"

        res = requests.get('http://localhost:8081/health', headers={'Authorization': 'Bearer secret'})
        assert res.status_code == 200
        assert res.content == b'OK'

        res = requests.get('http://localhost:8081/health', headers={'Authorization': 'Bearer wrong'})
        assert res.status_code == 401
        assert res.json()["code"] == "UNAUTHORIZED"

    def test_server_export(self):
        with self.runner.isolated_filesystem() as tmpdir:
            p = Process(target=server.serve, args=('localhost', 8082, None, f'{tmpdir}', {"home": ''}))
            p.start()
            for _ in range(5):
                time.sleep(0.2)
                try:
                    res = requests.get('http://localhost:8082/health')
                except:
                    pass
                else:
                    break

            init_project(
                self.runner,
                args=["init", "--mono"],
                env=dict(UNFURL_HOME=""),
            )

            res = requests.get('http://localhost:8082/export')
            assert res.status_code == 200
            exported = run_cmd(self.runner, ["--home", "", "export", "--format", "deployment"], env={"UNFURL_LOGGING": "critical"})
            assert exported
            assert res.json() == json.loads(exported.output)

            res = requests.get('http://localhost:8082/export?format=environments')
            assert res.status_code == 200
            exported = run_cmd(self.runner, ["--home", "", "export", "--format", "environments"], env={"UNFURL_LOGGING": "critical"})
            assert exported
            assert res.json() == json.loads(exported.output)

            res = requests.get('http://localhost:8082/export?format=blueprint')
            assert res.status_code == 200
            exported = run_cmd(self.runner, ["--home", "", "export", "--format", "blueprint"], env={"UNFURL_LOGGING": "critical"})
            assert exported
            assert res.json() == json.loads(exported.output)

            p.terminate()



    def tearDown(self) -> None:
        self.server_process.terminate() # Gracefully shutdown the server (SIGTERM)
        self.server_process.join() # Wait for the server to terminate
        return super().tearDown()

