Processing
==========

1. YAML parsed
2. Pre-processing of merge directives
3. Validation and instantiation of the model
4. Job is run
5. Unfurl expressions, TOSCA functions, and template strings are lazily evaluated as the job run

YAML pre-processing with merge directives
-----------------------------------------

When a YAML configuration is loaded, will look for dictionary keys that match the following pattern:

``'+'['?']['include'][*anchor][relative path][absolute path]``

and update the dictionary by processing the directive and merging in its resolved value.

.. productionlist::
     merge key      : "+"["?"]["include"][anchor][relative_path][absolute_path]
     anchor         : "*"[PCHAR]*[PCHAR except "."]
     relative_path  : "."+
     absolute_path  : ["/" PCHAR*]+
     PCHAR           : any printable character except "/"


Each of these components are optional but at least one needs to be present, otherwise the key is ignored and included in the final document.

A leading '?' indicates that reference maybe missing, otherwise the processing will abort with an error.

`include` indicates that value of the merge key includes a yaml or json file to load.

`*anchor`: a reference to YAML anchor that appears in either the current document or, a file was specified, from that file.

'relative path': '.'+ The path starting at current location this key appears in.

absolute path: [/path]+ A path that is resolved following <jsonpointer> RFC

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


Eval expressions
----------------

Expression Syntax
~~~~~~~~~~~~~~~~~~

.. productionlist::
     expr    : segment? ("::" segment)*
     segment : [key] ("[" filter "]")* ["?"]
     key     : name | integer | var | "*"
     filter  : ['!'] [expr] [("!=" | "=") test]
     test    : var | ([^$[]:?])+
     var     : "$" name

Function Syntax
~~~~~~~~~~~~~~~~

.. productionlist::
    eval    : expr | func
    vars    : dict
    trace   : integer
    foreach : (key?, value?) | expr
    func    : args
    kw      : dict
    args    : any

Semantics
~~~~~~~~~

Each segment specifies a key in a resource or JSON/YAML object.
"::" is used as the segment deliminated to allow for keys that contain "." and "/"

Path expressions evaluations always start with a list of one or more Resources.
and each segment selects the value associated with that key.
If segment has one or more filters
each filter is applied to that value -- each is treated as a predicate
that decides whether value is included or not in the results.
If the filter doesn't include a test the filter tests the existence or non-existence of the expression,
depending on whether the expression is prefixed with a "!".
If the filter includes a test the left side of the test needs to match the right side.
If the right side is not a variable, that string will be coerced to left side's type before comparing it.
If the left-side expression is omitted, the value of the segment's key is used and if that is missing, the current value is used.

If the current value is a list and the key looks like an integer
it will be treated like a zero-based index into the list.
Otherwise the segment is evaluated again all values in the list and resulting value is a list.
If the current value is a dictionary and the key is "*", all values will be selected.

If a segment ends in "?", it will only include the first match.
In other words, "a?::b::c" is a shorthand for "a[b::c]::0::b::c".
This is useful to guarantee the result of evaluating expression is always a single result.

The first segment:
If the first segment is a variable reference the current value is set to that variable's value.
If the key in the first segment is empty (e.g. the expression starts with '::') the current value will be set to the evaluation of '.all'.
If the key in the first segment starts with '.' it is evaluated against the initial "current resource".
Otherwise, the current value is set to the evaluation of ".ancestors?". In other words,
the expression will be the result of evaluating it against the first ancestor of the current resource that it matches.

If key or test needs to be a non-string type or contains a unallowed character use a var reference instead.

When multiple steps resolve to lists the resultant lists are flattened.
However if the final set of matches contain values that are lists those values are not flattened.

For example, given:

.. code-block:: javascript

 {x: [ {
         a: [{c:1}, {c:2}]
       },
       {
         a: [{c:3}, {c:4}]
       }
     ]
 }

`x:a:c` resolves to:
 `[1,2,3,4]`
not
 `[[1,2], [3,4]])`

(Justification: It is inconvenient and fragile to tie data structures to the particular form of a query.
If you want preserve structure (e.g. to know which values are part
of which parent value or resource) use a less deep path and iterate over results.)


Special keys
~~~~~~~~~~~~~
Instances have a special set of keys:

============ ======================================================
**.**            self
**..**          parent
.parents     list of parents
.ancestors   self and parents
.root        root ancestor
.children    child resources
.descendents (including self)
.all         dictionary of child resources with their names as keys
============ ======================================================
