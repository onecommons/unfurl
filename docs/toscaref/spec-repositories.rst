Repositories
============

``repositories`` refers to a named external repository which contains deployment and implementation artifacts that are referenced within the TOSCA Service Template.


Declaration
+++++++++++

.. code:: yaml

 <repository_name>:

   description: <repository_description>

   url: <repository_address>

   credential: <authorization_credential>


Example
++++++++

.. code:: yaml

 repositories:

   my_code_repo:

     description: My project’s code repository in GitHub

     url: https://github.com/my-project/