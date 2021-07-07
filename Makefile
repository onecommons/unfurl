ENV := py39

test: test-parallel test-serial

test-parallel:
	tox -e $(ENV) -- --no-cov -n auto \
	  tests/test_ansible.py \
	  tests/test_cli.py \
	  tests/test_configurator.py \
	  tests/test_decorators.py \
	  tests/test_dependencies.py \
	  tests/test_docker.py \
	  tests/test_docker_cmd.py \
	  tests/test_docs.py \
	  tests/test_eval.py \
	  tests/test_k8s.py \
	  tests/test_logs.py \
	  tests/test_runtime.py \
	  tests/test_shell.py \
	  tests/test_supervisor.py \
	  tests/test_syntax.py \
	  tests/test_terraform.py \
	  tests/test_tosca.py \
	  tests/test_undeploy.py

test-serial:
	tox -e $(ENV) -- --no-cov \
	  tests/test_configchanges.py \
	  tests/test_examples.py \
	  tests/test_helm.py \
	  tests/test_repo.py
