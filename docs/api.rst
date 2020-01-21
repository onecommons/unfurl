Merge directive
================

.. productionlist::
     key            : "+"["?"]["include"][anchor][relative_path][absolute_path]
     anchor         : "*"[PCHAR]*[PCHAR except "."]
     relative_path  : "."+
     absolute_path  : ["/" PCHAR*]+
     PCHAR           : any printable character except "/"

Expression syntax
==================

.. productionlist::
     expr    : segment? ("::" segment)*
     segment : [key] ("[" filter "]")* ["?"]
     key     : name | integer | var | "*"
     filter  : ['!'] [expr] [("!=" | "=") test]
     test    : var | ([^$[]:?])+
     var     : "$" name

Python API
===================================

.. automodule:: unfurl
  :members:

Manifest
--------
.. automodule:: unfurl.manifest
  :members:

.. automodule:: unfurl.yamlmanifest
  :members:

Configurators
-------------
.. automodule:: unfurl.configurator
  :members:

Runtime
-------
.. automodule:: unfurl.runtime
  :members:

Eval
----
.. automodule:: unfurl.eval
  :members:

Util
----
.. automodule:: unfurl.util
  :members:
