Quick start
===========

1. After you've installed Unfurl, the first step is to create your "home" environment for Unfurl:

.. code-block:: shell

    unfurl home --init

This will create an Unfurl project that, by default, is located at ``~/.unfurl_home``. It will contain local configuration settings that will shared with your other projects and also creates an isolated environment to run Unfurl in. For more information, see `.unfurl_home<configure>`.

2. Create an Unfurl project that will manage your deployment environments and record changes to your cloud accounts, for example:

.. code-block:: shell

    unfurl init --create-environment aws-staging --skeleton aws

The ``--skeleton`` option lets you specify an alternative to the default project skeleton. Unfurl currently includes two: ``gcp`` and ``aws``.

.. important::

  Store the master password found in ``local/unfurl.yaml`` in a safe place! By default this password is used to encrypt any sensitive data committed to repository. See :doc:`secrets` for more information.

3. (Optional) Create a separate project to manage your application's configuration and infrastructure-as-code files.

You can add your infrastructure-as-code configuration directly into the environments project you just created but it can be more flexible to create separate projects for each application you are developing. For example, if you already have a git repository containing your application's code, this command:

.. code-block:: shell

    unfurl init --existing --use-environment aws-staging

will commit an Unfurl project located at ``.unfurl`` in git repository the current directory is located in. By default, it will deploy into the environment you specified with ``--use-environment`` option (here, ``aws-staging``) and those changes will be recorded in that environment's repository.

Now you are ready to start building your ensemble! For example, check out our examples for adding `Ansible` playbooks, `Terraform` modules or invoking `shell` commands. Or continue reading for a more in-depth Getting Started guide.
