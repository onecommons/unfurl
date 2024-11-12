.. _tosca_repositories:

Repositories
============

``repositories`` refers to a named `repository` which contains deployment and implementation artifacts that are referenced within the TOSCA Service Template.

A ``repository`` is a TOSCA abstraction that can represent any collection of artifacts. For example, a repository can be git repository, an OCI (Docker) container registry, or a local file directory -- see the `repository` section on using them with Unfurl.

If the repository name matches the package id syntax, Unfurl will treat repository declarations as a `package rule<package rules>`.

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


.. seealso:: For more information, refer to :tosca_spec2:`TOSCA Repository Section <_Toc50125248>`

