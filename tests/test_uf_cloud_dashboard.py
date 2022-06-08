import unittest
import os
import sys
import warnings
import json
from pathlib import Path
import shutil
import tempfile ### tried to use isolated_filesystem, but it was being deleted after context closed

import pytest

from .utils import isolated_lifecycle, DEFAULT_STEPS, _check_job, init_project
from click.testing import CliRunner
from unfurl.__main__ import cli, _latestJobs


"""
seems like this is happening elsewhere
def create_service_account_key():
	key = {}
	for field in ['type', 'project_id', 'private_key_id', 'private_key', 'client_email', 'client_id', 'auth_uri', 'token_uri', 'auth_provider_x509_cert_url', 'client_x09_cert_url']:
		name = 'UNFURL_TEST_service_account_key__' + field
		key[field] = os.getenv(name)
	sa_key_path = Path(tempfile.gettempdir()) / 'service-account-key.json'
	with open(sa_key_path, 'w') as sa_key:
		sa_key.write(json.dumps(key))
	return str(sa_key_path)
"""


class UfCloudDeployment:
	def __init__(
		self,
		path: str,
		env=None,
		tmp_dir=None,
		undeploy=True,
	):
		self.path = path
		self.env = env
		self.tmp_dir = tmp_dir
		self.cli_runner = CliRunner()
		with open(Path(path) / "environments.json", "r") as environments_json:
			self.environments_json = json.loads(environments_json.read())

		self.deployment_path = next(iter(self.environments_json['DeploymentPath'].values()))
		self.environment = self.deployment_path['environment']
		self.variables = self.deployment_path['pipelines'][0]['variables']
		self.tmp_path = tempfile.mkdtemp()
		self.undeploy = undeploy


	def __enter__(self):
		shutil.copytree(self.path, self.tmp_path, dirs_exist_ok=True)
		env = {**self.variables, **self.env}
		print("ENV ==" )
		print(json.dumps(env, indent=4))
		os.chdir(self.tmp_path)

		result = self.cli_runner.invoke(
			cli,
			[
				"--home", "''", "clone",
				"--existing", "--overwrite", "--mono",
				"--use-environment", env["DEPLOY_ENVIRONMENT"],
				"--use-deployment-blueprint", env["DEPLOYMENT"],
				"--skeleton", "dashboard",
				env["BLUEPRINT_PROJECT_URL"],
				env["DEPLOY_PATH"]
			],
			env=env
		)
		assert result.exit_code == 0, result

		result = self.cli_runner.invoke(
			cli,
			[
				"--home", "''", "deploy",
				"--use-environment", env["DEPLOY_ENVIRONMENT"],
				"--approve",
				"--jobexitcode", "error",
				env["DEPLOY_PATH"]
			],
			env=env
		)
		assert result.exit_code == 0, result

		result = self.cli_runner.invoke(
			cli,
			[ "--home", "''", "export", env["DEPLOY_PATH"] ],
			env=env
		)
		assert result.exit_code == 0, result
		with open(Path(self.tmp_path) / env["DEPLOY_PATH"] / "ensemble.json", "w") as ensemble_json:
			ensemble_json.write(result.output)

		result = self.cli_runner.invoke(
			cli,
			["commit", '-m"deploy"'],
			env=env
		)
		assert result.exit_code == 0, result

		return self

	def __exit__(self, bla, blah, blaaah):
		os.chdir(self.tmp_path)
		env = {**self.variables, **self.env}
		if self.undeploy:
			result = self.cli_runner.invoke(
				cli,
				[
					"--home", "''", "undeploy",
					"--use-environment", env["DEPLOY_ENVIRONMENT"],
					"--approve",
					"--jobexitcode", "error",
					env["DEPLOY_PATH"]
				],
				env=env
			)
			assert result.exit_code == 0, result

			result = self.cli_runner.invoke(
				cli,
				[ "--home", "''", "export", env["DEPLOY_PATH"] ],
				env=env
			)
			assert result.exit_code == 0, result
			with open(Path(self.tmp_path) / env["DEPLOY_PATH"] / "ensemble.json", "w") as ensemble_json:
				ensemble_json.write(result.output)


			result = self.cli_runner.invoke(
				cli,
				["commit", '-m"undeploy"'],
				env=env
			)
			assert result.exit_code == 0, result

class DashboardTest(unittest.TestCase):
	def setUp(self):
		#self.sa_key = create_service_account_key()
		self.env = {
			#"GOOGLE_APPLICATION_CREDENTIALS": self.sa_key,
		}
		for (key, value) in os.environ.items():
			self.env[key.replace('UNFURL_TEST_', '')] = value

	def test_simple_baserow_gcp(self):
		src_path = str(Path(__file__).parent / "fixtures" / "dashboards" / "simple-baserow-gcp")
		#env = {"UNFURL_MOCK_DEPLOY": "true"}
		env = {**self.env}
		with UfCloudDeployment(path=src_path, env=env, undeploy=False) as deployment:
			pass
