.. _yaml_merge_directives:

=====================
YAML Merge directives
=====================

When a YAML configuration is loaded, will look for dictionary keys that match the following pattern:

``'+'['?']['include'][*anchor][relative path][absolute path]``

and treat them as merge directives that update the dictionary by processing the directive and merging in its resolved value.

.. productionlist::
     merge key      : "+"["?"]["include"][anchor][relative_path][absolute_path]
     anchor         : "*"[PCHAR]*[PCHAR except "."]
     relative_path  : "."+
     absolute_path  : ["/" PCHAR*]+
     PCHAR           : any printable character except "/"


Each of these components are optional but at least one needs to be present, otherwise the key is ignored and included in the final document.

A leading '?' indicates that reference maybe missing, otherwise the processing will abort with an error.

``include`` indicates that value of the merge key includes a yaml or json file to load.

``*anchor``: a reference to YAML anchor that appears in either the current document or, a file was specified, from that file.

``relative path``: '.'+ The path starting at current location this key appears in.

``absolute path``: [/path]+ A path that is resolved following <jsonpointer> RFC

The value of the merge key:

If the directive contains "include" and the value is a string, treat the value as a file path or a URL to a YAML or JSON file.

Otherwise, if empty, perform the default merge behavior. If set to "raw", include the value without any further processing.

The resolved value is merged into the directive's dictionary using the following rules:

  If the result of the lookup is not a JSON object or YAML map:
    if the map containing the merge key has no other keys, it will replaced by the result, otherwise abort processing with a merge error
    if the map being replaced appears as an item in a list and the result of the merge is also a list, the list is spliced in place.
    (If you don't want that behavior just wrap the include in another list, e.g "[{+/list1: null}]")

  otherwise recursively merge the maps:
    for each key in the result object:
      if the key doesn't exist in the target:
        add the key and value
      if value of the target key is not an object,
        ignore this
      else:
        target[key] = merge(target[key], resolved[key])

Restoring merge directives
~~~~~~~~~~~~~~~~~~~~~~~~~~
When saving YAML config file that contained merge directive will attempt restore them even if the configuration has changed -- if the target object has changed, a new diff will be generated to reflect those changes.

Ansible Jinja2 Templates
========================

Filters
-------

  :eval: Evaluates the given `expression <eval expressions>` or function
  :mapValue: Resolves any `eval expressions` or template strings in the give map or list.
  :abspath: see `abspath`,
  :get_dir: see `get_dir`
  :which: Returns the full path to the given executable, like the ``which`` shell command.

Lookup plugins
--------------

  :unfurl: Evaluates the given `expression <eval expressions>`
           For example: ``{{ lookup("unfurl", "::instance1::anAttribute") }}``
