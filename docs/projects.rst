===============
Unfurl Projects
===============

  * Contexts
  * Local
  * Secrets and sensitive data
  * External (rename Imports)
  * Unfurl Home

Contexts
========

Connections
-----------

You configure how to connect to online cloud providers and services by creating
`relationship templates` in the local ensemble, which is TOSCA's way of specifying how instances connect.

Cloud provider instances are identified by context independent attributes such as an account id (usually automatically detected) as opposed to client-dependent connection attributes such as an API key. But including cloud provider instances in your specification is optional if the connection info is just passed through to external tools.

External ensembles
------------------

The `external` section of the manifest lets you declare instances that are imported from external manifests. Instances listed here can be accessed in two ways: One, they will be implicitly used if they match a node template that is declared abstract using the "select" directive (see "3.4.3 Directives"). Two, they can be explicitly referenced using the `external` expression function.

There are 3 instances that are always implicitly imported even if they are not declared:

- The `localhost` instance that represents the machine Unfurl is currently executing on. This instance is accessed through the `ORCHESTRATOR` keyword in TOSCA and is defined in the home manifest that resides in your Unfurl home folder.

- The `local` and `secret` instances are for representing properties that may be specific to the local environment that the manifest is running in and are declared in the local configuration file. All `secret` attribute values are treated as `sensitive` and redacted when necessary. These instances can be accessed through the `local` and `secret` expression functions (which are just shorthands for the `external` function).

Merging and precedence
----------------------
# the contexts defined here are merged with each project's and ensemble's contexts
# merge priority (from highest to lowest):
# (named context in project, named context in home,
#  defaults in project, defaults in home, context in ensemble)
  # the following local settings and environment variables are consumed
  # by the localhost's connection templates, set as needed

Environment
===========

You can control the environment variables are available while Unfurl is running
by the setting the `environment` directive.
When set in a `context` object it is applied globally or it can be appear in
the operation's `implementation` declaration where it will only be applied to that operation.

In either case, `environment` makes a copy of the current environment and applied each of its keys
in the order they are declared, adding the given key and value as
envirnoment variables execpt keys starting with "+" and "-"
will copy or remove the variable from the current into environment
into the new one. In that case "*" and "?" are treated like filename wildcards and "!" negates the match:

.. code-block:: YAML

  name: value    # add name=value
  +name:         # copy name into the enviroment
  +name: default # copy value, set it to "default" if not present
  +!prefix*:     # copy all except variables matching "prefix*"
  -!name:        # remove all except name
  -!prefix*:     # remove all except variables matching "prefix*"
  ^name: /bin  # treat name like a PATH and prepend value: e.g. /bin:$name


For example:

.. code-block:: YAML

  environment:
     -*:       # this will remove all environment variables
     +HOME:    # add HOME back
     FOO: bar  # set FOO = bar

The following environment variables will always be copied from the parent environment unless explicitly removed or set:

.. documentedlist::
   :listobject: unfurl.util._sphinx_envvars
   :header: "Name"
