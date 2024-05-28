import traceback
from unfurl.localenv import LocalEnv
from unfurl.server.__main__ import _apply_environment_patch
from click.testing import CliRunner
from unfurl.testing import run_cmd
from unfurl.to_json import to_environments


create_k8s = {
    "branch": "main",
    "latest_commit": "e2c47cf5318005e1fb9d1384b58b4ee776bf78a0",
    "patch": [
        {
            "name": "k8s",
            "primary_provider": {
                "name": "primary_provider",
                "type": "unfurl.relationships.ConnectsTo.K8sCluster",
                "__typename": "ResourceTemplate",
                "_sourceinfo": {
                    "file": "tosca_plugins/k8s.yaml",
                    "repository": "unfurl",
                    "url": "github.com/onecommons/unfurl",
                },
            },
            "instances": {
                "k8sDefaultIngressController": {
                    "type": "KubernetesIngressController",
                    "name": "k8sDefaultIngressController",
                    "title": "KubernetesIngressController",
                    "__typename": "ResourceTemplate",
                    "properties": [{"name": "annotations", "value": {}}],
                    "dependencies": [],
                    "_sourceinfo": {
                        "file": "k8s.py",
                        "url": "https://unfurl.cloud/onecommons/std.git",
                    },
                }
            },
            "connections": {
                "primary_provider": {
                    "name": "primary_provider",
                    "type": "unfurl.relationships.ConnectsTo.K8sCluster",
                    "__typename": "ResourceTemplate",
                    "_sourceinfo": {
                        "file": "tosca_plugins/k8s.yaml",
                        "repository": "unfurl",
                        "url": "github.com/onecommons/unfurl",
                    },
                }
            },
            "__typename": "DeploymentEnvironment",
        }
    ],
    "commit_msg": "Create environment 'k8s' in user-20240215T230610703Z/dashboard",
}

localConfig = """
apiVersion: unfurl/v1alpha1
kind: Project
environments:
  defaults:
    repositories:
      std:
        url: https://unfurl.cloud/onecommons/std.git
"""

patched = {
    "apiVersion": "unfurl/v1alpha1",
    "kind": "Project",
    "+?include-secrets": "secrets/secrets.yaml",
    "+?include-local": "local/unfurl.yaml",
    "environments": {
        "defaults": {
            "secrets": {"vault_secrets": {"default": None}},
            "repositories": {"std": {"url": "https://unfurl.cloud/onecommons/std.git"}},
        },
        "k8s": {
            "instances": {
                "k8sDefaultIngressController": {
                    "type": "k8s.KubernetesIngressController",
                    "properties": {"annotations": {}},
                    "metadata": {"title": "KubernetesIngressController"},
                }
            },
            "connections": {
                "primary_provider": {
                    "type": "k8s.unfurl.relationships.ConnectsTo.K8sCluster"
                }
            },
            "repositories": {},
            "imports": [
                {"file": "k8s.py", "namespace_prefix": "k8s", "repository": "std"},
                {
                    "file": "tosca_plugins/k8s.yaml",
                    "namespace_prefix": "k8s",
                    "repository": "unfurl",
                },
            ],
        },
    },
}


def test_create_env():
    runner = CliRunner()
    with runner.isolated_filesystem():
        args = [
            "init",
            "--skeleton",
            "dashboard",
        ]
        run_cmd(runner, args)
        local_env = LocalEnv()
        _apply_environment_patch(create_k8s["patch"], local_env)
        local_env.project.localConfig.config.save()
        v1 = local_env.project.localConfig.config.config
        assert v1["environments"]["k8s"] == patched["environments"]["k8s"]

        local_env = LocalEnv()  # reload
        _apply_environment_patch(create_k8s["patch"], local_env)

        local_env.project.localConfig.config.save()
        # should not have changed
        assert (
            local_env.project.localConfig.config.config["environments"]["k8s"]
            == v1["environments"]["k8s"]
        )
        assert (
            local_env.project.localConfig.config.config["environments"]["k8s"]
            == v2["environments"]["k8s"]
        )
        # exported = to_environments(LocalEnv(), "1")
        # print( exported["ResourceType"]["KubernetesIngressController@unfurl.cloud/onecommons/std:k8s"] )


v2 = {
    "apiVersion": "unfurl/v1alpha1",
    "kind": "Project",
    "+?include-secrets": "secrets/secrets.yaml",
    "+?include-local": "local/unfurl.yaml",
    "environments": {
        "defaults": {
            "secrets": {"vault_secrets": {"default": None}},
            "repositories": {"std": {"url": "https://unfurl.cloud/onecommons/std.git"}},
        },
        "k8s": {
            "instances": {
                "k8sDefaultIngressController": {
                    "type": "k8s.KubernetesIngressController",
                    "properties": {"annotations": {}},
                    "metadata": {"title": "KubernetesIngressController"},
                }
            },
            "connections": {
                "primary_provider": {
                    "type": "k8s.unfurl.relationships.ConnectsTo.K8sCluster"
                }
            },
            "repositories": {},
            "imports": [
                {"file": "k8s.py", "namespace_prefix": "k8s", "repository": "std"},
                {
                    "file": "tosca_plugins/k8s.yaml",
                    "namespace_prefix": "k8s",
                    "repository": "unfurl",
                },
            ],
        },
    },
}
