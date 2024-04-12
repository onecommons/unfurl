.. _tosca_repositories:

Repositories
============

``repositories`` refers to a named `repository <repository>` which contains deployment and implementation artifacts that are referenced within the TOSCA Service Template.

If the repository name matches the package id syntax, Unfurl will treat repository declaration as a package rule.

Declaration
+++++++++++

.. code:: yaml

 <repository_name>:

   url: <repository_address>

   # optional keys:

   description: <repository_description>

   credential: <authorization_credential (see tosca.datatypes.Credential)>

   # optional Unfurl extensions:

   revision: <revision>

   metadata: <metadata>


Example
++++++++

.. code:: yaml

 repositories:

   my_code_repo:

     description: My project’s code repository in GitHub

     url: https://github.com/my-project/