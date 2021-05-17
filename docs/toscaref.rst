================================
TOSCA Service Template Reference
================================

An application in Unfurl is described as a TOSCA 1.3 service template. 

:tosca_spec:`Service templates<DEFN_ELEMENT_SERVICE_TEMPLATE>` are written in YAML and describe the logical representation of an application, which we call a :tosca_spec:`topology <_Toc50125458>`. In a service template, you can describe the application's components, how they relate to one another, how they are installed and configured and how they're monitored and maintained.

Other than the YAML itself, a service template can comprise multiple resources such as configuration and installation scripts, code, and basically any other resource you require for running your application.

All files in the directory that contains the service template's main file, are also considered part of the service template, and paths described in the service template are relative to that directory. 


A service template is comprised of several high level sections:


.. toctree::
   :maxdepth: 2

   Versioning<toscaref/spec-tosca_def_version.rst>
   Repositories<toscaref/spec-repositories.rst>
   Imports<toscaref/spec-imports.rst>
   Node Types<toscaref/spec-node-types.rst>
   Data Types<toscaref/spec-data-types.rst>
   Other Types<toscaref/spec-other-types.rst>
   Topology Templates<toscaref/spec-topology-template.rst>
   



















