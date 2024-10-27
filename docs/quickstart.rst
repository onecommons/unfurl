Quick start
===========

This section will introduce you to Unfurl by creating a simple project that deploys a web application. Once you get this working, you can customize it for your own application. 

.. tip::
  The steps here follow the high-level overview of Unfurl found in the `solution-overview` section.

Step 1 Create a project
-----------------------

The first step is to create an Unfurl project to manage your deployments:

.. code-block:: shell

    unfurl init myproject --empty

This is will create an Unfurl project in directory "myproject".  The ``--empty`` option skips creating an ensemble (a deployment) for now (we'll add that later in `step 3<Step 3 Instantiate your blueprint>`).

If this is the first time you've created a Unfurl project, you'll notice a message like "Unfurl home created at ~/.unfurl_home".  Unfurl home is an Unfurl project that contains local settings and resources that are shared with other projects on that machine, including an isolated execution environment to run Unfurl in. For more information, see `Unfurl Home`.

Step 2: Describe your application
---------------------------------

Now that your project is set up, let's deploy a web app. Let's consider a simple nodejs app that connects to a Postgres database.

Our web app is a container image. 

* We to create a service that can run the container image.
* Need to have access to a database and connect to the database.
* It also needs to hooked up to DNS.

In ensemble-template.yaml we'll define our web app at a high-level.

The TOSCA specification defines types that provide basic abstractions for resources like compute instances and container images. In addition, we've developed the |stdlib|_.

1. Open ``ensemble-template.yaml`` and uncomment these lines:

.. code-block:: yaml

    repositories:
      std:
        url: https://unfurl.cloud/onecommons/std.git

2. If you want to follow along using the Python examples, open ``service_template.py`` and uncomment these lines:

.. code-block:: python

  import tosca
  from tosca_repositories import std

3. Run ``unfurl validate``. 

This will make sure the changes you just made are valid but more importantly, as a side effect, it will install the ``std`` repository so that if you open ``service_template.py`` in an Python IDE such as VS Code the Python import statements will resolve, enabling your editor's type checking and code navigation.

4. Add the blueprint.

For our example, we'll use these types to model our application:
Add this to either service_template.py or ensemble-template.yaml:

.. tab-set-code::

  .. literalinclude:: ./examples/quickstart_service_template.py
    :language: python

  .. literalinclude:: ./examples/quickstart_service_template.yaml
    :language: yaml

Step 3 Instantiate your blueprint
---------------------------------

Now we have a model that we can customize for different environments.
In this example, let's suppose there are two types of environments we want to deploy this into:

* a production environment that deploys to AWS and using AWS RDS database
* a development environments that runs the app and Postgres as services in a Kubernetes cluster.

Let's create those environments, along with a deployment for each:

.. code-block:: shell

   cd myproject
   unfurl init production --skeleton aws --use-environment production
   unfurl init development --skeleton k8s --use-environment development

The ``--skeleton`` option lets you specify an alternative to the default project skeleton. We'll assume we're deploying it into AWS so we will use the ``aws`` project skeleton. You can see all the built-in project skeletons :unfurl_github_tree:`here <unfurl/skeletons>` or use an absolute path to specify your own. 

.. important::

  Store the master password found in ``ensemble/local/unfurl.yaml`` in a safe place! By default this password is used to encrypt any sensitive data committed to repository. See :doc:`secrets` for more information.

There are different approaches to customize a blueprint but simple one is to declare deployment blueprints. A `deployment blueprint` is a blueprint that is only applied when its criteria matches the deployment environment. It inherits from the global blueprint and includes node templates that override the blueprint's.

Ensemble's ``deployment_blueprints``  In Python, a `deployment blueprint` is represented as a Python class with the customized template objects as class attributes.

Add the following code below the code from the previous step:

.. _deployment_blueprint_example:

.. tab-set-code::

  .. literalinclude:: ./examples/quickstart_deployment_blueprints.py
    :language: python

  .. literalinclude:: ./examples/quickstart_deployment_blueprints.yaml
    :language: yaml


Here we are using existing implementations defined in the std library -- to write your own, check out our examples for adding `Ansible` playbooks, `Terraform` modules or invoking `shell` commands. 

Now if we run :cli:`unfurl plan<unfurl-plan>`

Step 4. Deploy and manage
-------------------------

Now we're ready to deploy our application.
Run :cli:`unfurl deploy development<unfurl-deploy>` from the command line to deploy the development ensemble.

*  :cli:`unfurl commit<unfurl-commit>` It will commit to git the latest configuration and a history of changes to your cloud accounts. (Or you could have used the ``--commit`` flag with :cli:`unfurl depoy<unfurl-deploy>`)

* You can ``unfurl serve --gui`` Or host your repositories to `Unfurl Cloud`.

* If you make changes to your deployment will update it.

* Delete it using :cli:`unfurl teardown<unfurl-teardown>`.

Step 5. Share and Collaborate
-----------------------------

To share your blueprint and deployment, push your repository to a git host service such as Github or Gitlab (or better yet, `Unfurl Cloud`_!). You just have to `configure git remotes<Publishing your project>` for the git repositories we created.

When we ran :cli:`unfurl init<unfurl-init>`, we relied on the default behavior of creating a separate git repository for each ensemble. This allows the project's blueprints and deployments to have separate histories and access control.

We can make the blueprint repository public but limit access to the production repository to system admins. In either case, you'd use the `unfurl clone<Cloning projects and ensembles>` command to clone the blueprint or the ensemble.

If you want to create a new deployment from the blueprint, clone the blueprint repository, by default Unfurl will create a new ensemble using the blueprint unless the ``--empty`` flag is used.

If you want to manage one of the deployments we already deployed, clone the repository that has that ensemble. 

.. tip::

  If we had used ``--submodule`` option with :cli:`unfurl init<unfurl-init>` (or manually added a submodule using ``git submodule add``) then the unfurl clone command would have cloned those ensembles too as submodules.

Once multiple users are sharing your projects can start `exploring<step5>` the different ways you can collaborate together to develop and manage your blueprints and deployments.
