import logging
import unittest
import os
import json
import pickle
import io

import tosca
from unfurl.result import ResultsList, ResultsMap, serialize_value, ChangeRecord, Result
from unfurl.eval import Ref, UnfurlEvalError, analyze_expr, map_value, RefContext, set_eval_func, ExternalValue, SafeRefContext
from unfurl.support import apply_template, TopologyMap, _sandboxed_template
from unfurl.tosca_plugins.functions import to_dns_label
from unfurl.util import UnfurlError, sensitive_str, substitute_env, sensitive_list
from unfurl.runtime import NodeInstance
from ruamel.yaml.comments import CommentedMap

class EvalTest(unittest.TestCase):
    longMessage = True

    def test_CommentedMap(self):
        cm = CommentedMap()
        # check bug in ruamel.yaml is fixed: raises TypeError: source has undefined order
        self.assertEqual(cm, cm.copy())

    def test_CommentedMapEquality(self):
        cm = CommentedMap((("b", 2),))
        cm.insert(1, "a", 1, comment="a comment")
        self.assertEqual(cm, {"a": 1, "b": 2})

    def _getTestResource(self, more=None, parent=None):
        resourceDef = {
            "name": "test",
            "a": {"ref": "name"},
            "b": [1, 2, 3],
            "d": {"a": "va", "b": "vb"},
            "n": {"n": {"n": "n"}},
            "s": {"ref": "."},
            "x": [
                {
                    "a": [
                        {"c": 1},
                        {"c": 2},
                        {"b": "exists"},
                        {"l": ["l1"]},
                        {"l": ["l2"]},
                    ]
                },
                [{"c": 5}],
                {"a": [{"c": 3}, {"c": 4}, {"l": ["l3"]}, {"l": ["l4"]}]},
                [{"c": 6}],
            ],
            "e": {"a1": {"b1": "v1"}, "a2": {"b2": "v2"}},
            "f": {"a": 1, "b": {"ref": ".::f::a"}},
            "empty_list": [],
            "ports": ["80:81"]
        }
        if more:
            resourceDef.update(more)
        resource = NodeInstance("test", resourceDef, parent)
        assert resource.attributes["x"] == resourceDef["x"]
        assert resource.attributes["a"] == "test"
        assert resource.attributes["s"] is resource
        return resource

    def test_refs(self):
        assert Ref.is_ref({"ref": "::name"})
        assert not Ref.is_ref({"ref": "::name", "somethingUnexpected": 1})
        assert Ref.is_ref({"ref": "::name", "vars": {"a": None}})

    def test_refPaths(self):
        resource = self._getTestResource()
        for (exp, expected) in [
            ["x?::a[c=4]", [[{"c": 3}, {"c": 4}, {"l": ["l3"]}, {"l": ["l4"]}]]],
            [
                "x::a[c]?",
                [[{"c": 1}, {"c": 2}, {"b": "exists"}, {"l": ["l1"]}, {"l": ["l2"]}]],
            ],
            ["x::a::[c]", [{"c": 1}, {"c": 2}, {"c": 3}, {"c": 4}]],
            ["x::a?::[c]", [{"c": 1}, {"c": 2}]],
            ["a", ["test"]],
            ["b", [[1, 2, 3]]],
            ["b::0", [1]],
            ["b?::2", [3]],
            ["[b::2]::b::2", [3]],
            ["b::1", [2]],
            ["s::b", [[1, 2, 3]]],
            ["s::b::1", [2]],
            ["s::s::b::1", [2]],
            ["n::n::n", ["n"]],
            ["d[a=va]", [{"a": "va", "b": "vb"}]],
            ["d[a=vb]", []],
            ["b[1=2]", [[1, 2, 3]]],
            ["b[1=1]", []],
            ["a[=test]", ["test"]],
            ["a[!=test]", []],
            ["a[key]", []],
            ["d[a=va][b=vb]", [{"a": "va", "b": "vb"}]],
            ["d[a=va][a=vb]", []],
            ["d[a=va][a!=vb]", [{"a": "va", "b": "vb"}]],
            ["d[a=va]::b", ["vb"]],
            ["x::a::c", [1, 2, 3, 4]],
            ["x::c", [5, 6]],
            ["x::[c]", [{"c": 5}, {"c": 6}]],
            ["x::[c=5]", [{"c": 5}]],
            ["x::a[b]::c", [1, 2]],
            ["x::a[!b]::c", [3, 4]],
            ["x::a::l", [["l1"], ["l2"], ["l3"], ["l4"]]],
            [{"ref": "a[=$yes]", "vars": {"yes": "test"}}, ["test"]],
            [{"ref": "a[=$no]", "vars": {"no": None}}, []],
            ["[a]", [resource]],
            ["[=blah]", []],
            ["[blah]", []],
            ["[!blah]", [resource]],
            [".[!=blah]", [resource]],
            ["[!a]", []],
            ["::test", [resource]],
            ["d::*", set(["va", "vb"])],
            ["e::*::b2", ["v2"]],
            ["*", []],
            ["f", [{"a": 1, "b": 1}]],
            ["::*", [resource]],
            ["::*::.template::type", ["tosca.nodes.Root"]],
            ["$missing::a", []],


            # [{"q": "{{ foo }}"}, ["{{ foo }}"]]
            # XXX test nested ['.[k[d=3]=4]']
        ]:
            ref = Ref(exp)
            # print ('eval', ref.source, ref)
            result = ref.resolve(RefContext(resource, trace=0))
            assert all(not isinstance(i, Result) for i in result)
            if isinstance(expected, set):
                # for results where order isn't guaranteed in python2.7
                self.assertEqual(
                    set(result),
                    expected,
                    "expr was: " + ref.source,
                )
            else:
                self.assertEqual(
                    result,
                    expected,
                    "expr was: " + ref.source,
                )

    def test_last_resource(self):
        parent = NodeInstance("parent")
        self._getTestResource(parent=parent)
        NodeInstance("another_child", parent=parent)
        ref = Ref(".::.instances::x::c")
        ctx = RefContext(parent, trace=0)
        # index = ctx.referenced.start()
        result = ref.resolve(ctx)
        assert(ctx._lastResource.name) == "parent"
        assert result == [5, 6]

    def test_funcs(self):
        resource = self._getTestResource()
        test1 = {"ref": ".name", "vars": {"a": None}}
        test2 = {"ref": "$b", "vars": {"b": 1}}
        test3 = {
            "ref": {
                "if": {"not": "$a"},
                "then": {"q": "expected"},
                "else": {"q": "unexpected"},
            },
            "vars": {"a": None},
        }
        result1 = Ref(test1).resolve_one(RefContext(resource))
        self.assertEqual("test", result1)
        result2 = Ref(test2).resolve_one(RefContext(resource))
        self.assertEqual(1, result2)
        result3 = Ref(test3).resolve_one(RefContext(resource))
        self.assertEqual("expected", result3)
        result4 = Ref(test3).resolve(RefContext(resource))
        self.assertEqual(["expected"], result4)
        test5 = {"ref": {"or": ["$a", "b"]}, "vars": {"a": None}}
        result5 = Ref(test5).resolve_one(RefContext(resource))
        assert all(not isinstance(i, Result) for i in result5)
        self.assertEqual(
            resource.attributes["b"], result5
        )  # this doesn't seem obvious!
        test6 = {"eval": {"add": [1, 2]}}
        assert 3 == Ref(test6).resolve_one(RefContext(resource))
        test7 = {"eval": {"sub": [1, 1]}}
        assert 0 == Ref(test7).resolve_one(RefContext(resource))
        with tosca.set_evaluation_mode("runtime"):
            test8 = {"eval":  dict(scalar_value="6 mb", unit="gb", round=2)}
            assert 0.01 == Ref(test8).resolve_one(RefContext(resource))
            assert Ref(dict(eval=dict(generate_string=None,preset= "password"))).resolve_one(RefContext(resource))
            assert Ref(dict(eval=dict(_generate={ "preset": "password" }))).resolve_one(RefContext(resource))

    def test_circular_refs(self):
        more = {}
        more["circular_a"] = dict(eval=".::circular_b")
        more["circular_b"] = dict(eval=".::circular_a")
        more["circular_c"] = dict(eval={"or": [".::circular_d", 1]})
        more["circular_d"] = dict(eval={"or": [".::circular_c", 1]})

        resource = self._getTestResource(more)
        assert resource.attributes["circular_a"] == None, resource.attributes["circular_a"]
        assert resource.attributes["circular_b"] == None, resource.attributes["circular_b"]
        assert resource.attributes["circular_c"] == 1, resource.attributes["circular_c"]
        assert resource.attributes["circular_d"] == 1, resource.attributes["circular_d"]

    def test_forEach(self):
        resource = self._getTestResource()
        test1 = {"ref": ".", "select": {"value": {"content": {"ref": "b"}}}}
        expected0 = {"content": [1, 2, 3]}
        result0 = Ref(test1).resolve_one(RefContext(resource, trace=0))
        self.assertEqual(expected0, result0)
        # resolve has same result as resolveOne
        self.assertEqual([expected0], Ref(test1).resolve(RefContext(resource)))

        # add 'key' to make result a dict
        # test that template strings work
        # XXX fragile: key is base64 of __str__ of NodeInstance
        test1["select"]["key"] = "{{ item | ref | b64encode}}"
        result1 = Ref(test1).resolve_one(RefContext(resource))
        expected = {"Tm9kZUluc3RhbmNlKCd0ZXN0Jyk=": expected0}
        self.assertEqual(expected, result1, result1)
        result2 = Ref(test1).resolve(RefContext(resource))
        self.assertEqual([expected], result2)

        test2 = {
          "eval": ".::b",
          "foreach": "{{ item * 2 }}"
        }
        result3 = Ref(test2).resolve_one(RefContext(resource, trace=0))
        assert result3 == [2, 4, 6]

        test3 = {
          "eval": "a",
          "foreach": "$item"
        }
        result4 = Ref(test3).resolve_one(RefContext(resource, trace=0))
        assert result4 == ["test"]

        test3 = {
          "eval": "a",
          "foreach": "$true"
        }
        result4 = Ref(test3).resolve_one(RefContext(resource, trace=0))
        assert result4 == ["test"]

        test4 = {
          "eval": "empty_list",
          "foreach": "$item"
        }
        result5 = Ref(test4).resolve_one(RefContext(resource, trace=0))
        assert result5 == []

        test5 = {
            "eval": {"portspec": "80:81"},
            "select": "source"
        }
        result6 = Ref(test5).resolve_one(RefContext(resource, trace=0))
        assert result6 == 80

        test6 = {
            "eval": {"portspec": "80:81"},
            "select": "target"
        }
        result7 = Ref(test6).resolve_one(RefContext(resource, trace=0))
        assert result7 == 81

        from toscaparser.elements.portspectype import PortSpec

        test7 = {
            "eval": "$p",
            "foreach": {
                "eval": {"portspec": {"eval": "$item"}}
            }
        }
        result7 = Ref(test7).resolve_one(RefContext(resource, vars=dict(p="80:81"), trace=0))
        assert result7 == [PortSpec.make("80:81")]

        test8 = {
            "eval": ".::ports",
            "foreach": {
                "eval": {"portspec": {"eval": "$item"}}
            }
        }
        result8 = Ref(test8).resolve_one(RefContext(resource, trace=0))
        assert result8 == [PortSpec.make("80:81")]

        mapped = ResultsMap._map_value(dict(test8=test8), RefContext(resource))
        assert mapped["test8"] == [PortSpec.make("80:81")]


    def test_serializeValues(self):
        resource = self._getTestResource()
        src = {"a": ["b", resource]}
        serialized = serialize_value(src)
        self.assertEqual(serialized, {"a": ["b", {"ref": "::test"}]})
        self.assertEqual(src, map_value(serialized, resource))
        serialized = serialize_value(dict(foo=sensitive_str("sensitive"),yes='yes'), redact=True)
        self.assertEqual(json.dumps(serialized), '{"foo": "<<REDACTED>>", "yes": "yes"}')

    def test_map_value(self):
        resource = self._getTestResource()
        result = {'outputs': {'ec2_instance':
                      {'ami': 'ami-0077f1602df963b17',
                       'arn': 'arn:aws:ec2:eu-central-1',
                       'id': 'i-087439ef0d1105c1c' }
                   }
        }
        ctx = RefContext(resource)
        resultTemplate = ResultsMap(dict(attributes=dict(
                  id = "{{ outputs.ec2_instance.id }}",
                  arn = "{{ outputs.ec2_instance.arn }}")), ctx)
        # if don't get _attributes here this fails because it resolve's against ctx's vars:
        results = map_value(resultTemplate._attributes, ctx.copy(vars=result))
        assert results == {'attributes': {'id': 'i-087439ef0d1105c1c', 'arn': 'arn:aws:ec2:eu-central-1'}}

    def test_jinjaTemplate(self):
        resource = NodeInstance("test", attributes=dict(a1="hello"))
        ctx = RefContext(resource, {"foo": "hello"})

        self.assertEqual(apply_template(" {{ foo }} ", ctx), "hello")
        # test jinja2 native types
        self.assertEqual(apply_template(" {{[foo]}} ", ctx), ["hello"])

        self.assertEqual(apply_template(' {{ "::test::a1" | ref }} ', ctx), u"hello")
        self.assertEqual(
            apply_template(' {{ lookup("unfurl", "::test::a1") }} ', ctx), u"hello"
        )
        # ansible query() always returns a list
        self.assertEqual(
            apply_template('{{  query("unfurl", "::test::a1") }}', ctx), [u"hello"]
        )

        os.environ[
            "TEST_ENV"
        ] = "testEnv"  # note: tox doesn't pass on environment variables so we need to set one now
        self.assertEqual(
            map_value("{{ lookup('env', 'TEST_ENV') }}", resource), "testEnv"
        )

        self.assertEqual(
            map_value("{{ lookup('env', 'MISSING') }}", resource), ""
        )

        # test that ref vars as can be used as template string vars
        exp = {"a": "{{ aVar }} world"}
        vars = {"aVar": "hello"}
        self.assertEqual(
            map_value(exp, RefContext(resource, vars)), {"a": "hello world"}
        )

        vars = {"foo": {"bar": sensitive_str("sensitive")}}
        val = apply_template("{{ foo.bar }}", RefContext(resource, vars, trace=0))
        assert isinstance(val, sensitive_str), type(val)

        val = map_value(
            "{{ {'key' : zone['n']} }}",  # n returns a ResultsMap
            RefContext(
                self._getTestResource(),
                {"zone": {"n": ResultsMap({"a": "b"}, RefContext(resource))}},
                trace=0,
            ),
        )
        assert val == {"key": {"a": "b"}}
        # actually {'key': Results({'a': Result('b', None, ())})}

        assert type(apply_template(" {{ {} }} ", RefContext(resource, vars))) == dict
        assert apply_template('{{ {} }}{{"\n "}}', RefContext(resource, vars)) == '{}\n '

        val = apply_template("{{ 'foo' | sensitive }}", RefContext(resource, trace=0))
        assert isinstance(val, sensitive_str), type(val)

        val = apply_template("{{ ['a', 'b' | sensitive] }}", RefContext(resource, trace=0))
        assert isinstance(val[1], sensitive_str), type(val)

        val = apply_template("{{ ['a', 'b'] | sensitive }}", RefContext(resource, trace=0))
        assert isinstance(val, sensitive_list), type(val)

        # test treating expression functions as RefContext methods
        val = map_value(
            "{{ __unfurl.to_label('a','b', sep='.') }}",
            ctx
        )
        assert val == "a.b"

        val = map_value(
            "{{ __unfurl.scalar_value('500 kb' | scalar + '1 mb' | scalar, 'gb') }}",
            ctx
        )
        assert val == 0.0015

    def test_templateFunc(self):
        query = {
            "eval": {"template": "{%if testVar %}{{success}}{%else%}failed{%endif%}"},
            "vars": {
                "testVar": True,
                "success": dict(eval={"if": "$true", "then": ".name"}),
            },
        }
        resource = self._getTestResource({"aTemplate": query, "q": dict(q="{{ SELF }}")})
        self.assertEqual(map_value(query, resource), "test")
        self.assertEqual(resource.attributes["aTemplate"], "test")
        self.assertEqual(resource.attributes["q"], "{{ SELF }}")

        template = """\
#jinja2: variable_start_string: '<%', variable_end_string: '%>'
{% filter from_yaml %}
a_dict:
    key1: "{{ extras.quoted }}" # shouldn't be evaluated
    key2: "<% extras.quoted %>" # will be evaluated to "{{ quoted }}  "
    key3: "original"
    <<: <% key4 | mapValue | to_json %>  # will merge into a_dict
{% endfilter %}
        """
        vars = {
            "extras": dict(key3="hello", quoted={"q": "{{ quoted }}  "}),
            "key4": dict(extra1={"a": 1}, extra2=[1, 3], key3="overwritten"),
            "key2": {"q": "{{ quoted2 }}  "},
        }
        query2 = {"eval": {"template": template}}
        expected = {
            "a_dict": {
                "key1": "{{ extras.quoted }}",
                "key2": "{{ quoted }}  ",
                "key3": "original",
                "extra1": {"a": 1},
                "extra2": [1, 3],
            }
        }
        ctx = RefContext(resource, vars)
        result = apply_template(template, ctx)
        self.assertEqual(result, expected)
        result = map_value(query2, ctx)
        self.assertEqual(result, expected)

    def test_templateNodes(self):
        resource = self._getTestResource()
        NODES = TopologyMap(resource)
        assert resource.attributes is NODES["test"]
        ctx = RefContext(resource, dict(NODES=NODES))
        self.assertEqual("va", apply_template("{{ NODES.test.d.a }}", ctx))

    def test_sandbox(self):
        resource = self._getTestResource()
        ctx = SafeRefContext(resource, vars=dict(subdomain="foo.com"))
        expr = "{{ {subdomain.split('.')[0] : 1} }}"
        result = _sandboxed_template(expr, ctx, ctx.vars, None)
        assert result == {'foo': 1}
        assert type(result) == dict
        with self.assertRaises(UnfurlEvalError) as err:
            map_value(dict(eval={"get_env": "HOME"}), ctx)
        assert 'function unsafe or missing in ' in str(err.exception)

        assert not map_value(dict(eval={"is_function_defined": "get_env"}), ctx)
        ctx2 = RefContext(resource, vars=dict(subdomain="foo.com"))
        assert map_value(dict(eval={"is_function_defined": "get_env"}), ctx2)

        ctx3 = SafeRefContext(resource, strict=False)
        assert map_value(dict(eval="{{ foo | to_json }}"), ctx3) == "<<Error rendering template: No filter named 'to_json'.>>"
        assert map_value(dict(eval="{{ foo | abspath }}"), ctx3) == "<<Error rendering template: No filter named 'abspath'.>>"

    def test_innerReferences(self):
        resourceDef = {
            "a": dict(b={"ref": "a::c"}, c={"e": 1}, d=["2", {"ref": "a::d::0"}])
        }
        resource = NodeInstance("test", attributes=resourceDef)
        assert not not resource.attributes
        assert len(resource.attributes) == 1

        expectedA = {"c": {"e": 1}, "b": {"e": 1}, "d": ["2", "2"]}
        self.assertEqual(resource.attributes["a"]["b"], expectedA["b"])
        self.assertEqual(resource.attributes["a"], expectedA)
        self.assertEqual(Ref("a").resolve(RefContext(resource)), [expectedA])
        self.assertEqual(Ref("a").resolve_one(RefContext(resource)), expectedA)

        expected = ["2"]
        self.assertEqual(Ref("a::d::0").resolve(RefContext(resource)), expected)
        self.assertEqual(Ref("a::d::1").resolve(RefContext(resource)), expected)

        # print('test_references', resource.attributes,
        #   'AAAA', resource.attributes['a'],
        #   'BBB', resource.attributes['a']['b'],
        # )
        self.assertEqual(resource.attributes["a"], expectedA)
        self.assertEqual(resource.attributes["a"]["d"][0], "2")
        self.assertEqual(resource.attributes["a"]["d"][1], "2")
        self.assertEqual(resource.attributes["a"]["b"]["e"], 1)

        self.assertEqual(Ref("a::b::e").resolve(RefContext(resource)), [1])

        # test again to make sure it still resolves correctly
        self.assertEqual(Ref("a::d::0").resolve(RefContext(resource)), expected)
        self.assertEqual(Ref("a::d::1").resolve(RefContext(resource)), expected)

    def test_vars(self):
        # test dereferencing vars
        resource = self._getTestResource()
        query = {
            "eval": "$aDict",
            "vars": {"aDict": {"aRef": {"eval": "::test"}, "aTemplate": "{{ true }}"}},
        }
        result = Ref(query).resolve_one(RefContext(resource))
        self.assertEqual(result, {"aRef": resource, "aTemplate": True})

        query = {"eval": "$aRef", "vars": {"aRef": {"eval": "::test"}}}
        assert Ref.is_ref(query["vars"]["aRef"])
        result = Ref(query).resolve_one(RefContext(resource))
        self.assertEqual(result, resource)

    def test_nodeTraversal1(self):
        root = NodeInstance(
            "r2", {"a": [dict(ref="::r1::a"), dict(ref="b")], "b": "r2"}  #'r1'  #'r2'
        )
        child = NodeInstance("r1", {"a": dict(ref="b"), "b": "r1"}, root)
        ctx = RefContext(root)
        x = [{"a": [{"c": 1}, {"c": 2}]}]
        r1 = ResultsList(x, ctx)
        assert x == r1
        self.assertEqual(Ref("b").resolve(RefContext(child)), ["r1"])
        self.assertEqual(Ref("a").resolve(RefContext(child)), ["r1"])
        self.assertEqual(Ref("a").resolve(RefContext(root)), [["r1", "r2"]])

        assert not r1._haskey(1)
        r1.append("added")
        assert r1._haskey(1)
        r1[0]["a"][1] = "not c"
        assert r1[0]["a"][1] == "not c"

    def test_nodeTraversal2(self):
        root = NodeInstance("root", {"a": [{"ref": "::child"}, {"b": 2}]})
        child = NodeInstance("child", {"b": 1}, root)
        self.assertEqual(Ref(".ancestors").resolve(RefContext(child)), [[child, root]])
        # self.assertEqual(Ref('a::b').resolve(RefContext(root)), [1])
        self.assertEqual(Ref("a").resolve(RefContext(child)), [[child, {"b": 2}]])
        # a resolves to [child, dict] so a::b resolves to [child[b], [b]2]
        self.assertEqual(Ref("a::b").resolve(RefContext(child)), [1, 2])

        root = NodeInstance("root", {"q": {"eval": "::child::q"},
                                     "q2": {"eval": "::child::q2"}
                                     }, None)
        child = NodeInstance("child", {
            "q": dict(q={"eval": ".name"}),
            "q2": "{{ '{' + '{' + 'SELF}}' }}"
            }, root)
        # XXX fix: assert root.attributes["q"] == {"eval": ".name"}
        assert root.attributes["q2"] == "{{SELF}}"

    def test_lookup(self):
        resource = self._getTestResource()
        os.environ[
            "TEST_ENV"
        ] = "testEnv"  # note: tox doesn't pass on environment variables so we need to set one now
        query = {"eval": {"lookup": {"env": "TEST_ENV"}}}
        self.assertEqual(map_value(query, resource), "testEnv")

    def test_tempfile(self):
        resource = self._getTestResource()
        value = {"a": 1}
        template = dict(
            eval={"template": "{{ valuesfile }}"},
            vars={"valuesfile": {"eval": {"tempfile": value}}},
        )
        result = map_value(template, resource)
        # result is a path to the tempfile
        with open(result) as tp:
            self.assertEqual(tp.read(), json.dumps(value, indent=2))

    def test_template_path(self):
        resource = self._getTestResource()
        template_contents = """\
{%   if str(image)  -%}
foo
{% endif %}
"""
        # write template_contents to a temp file and have the template function read that file
        template = dict(
            eval={"template": dict(path={"eval": {"tempfile": template_contents}})},
            vars={"image": "foo/bar"},
        )
        ctx = RefContext(resource, strict=False, trace=0)
        class mock_task:
            _errors = []
            logger = logging.getLogger("test")
        ctx.task = mock_task()
        assert not ctx.strict
        assert not ctx.task._errors
        result = map_value(template, ctx)
        # templating failed so it returned the original template 
        self.assertEqual(result, template_contents.strip(), len(result))
        assert ctx.task._errors

    def test_changerecord(self):
        assert ChangeRecord.is_change_id("A01110000005")
        assert not ChangeRecord.is_change_id("A0111000000"), "too short"
        assert not ChangeRecord.is_change_id(None), "not a string"
        assert not ChangeRecord.is_change_id(True), "not a string"

    def test_binaryvault(self):
        from unfurl.support import AttributeManager
        from unfurl.yamlloader import make_yaml, sensitive_bytes, make_vault_lib

        # load a binary file then write it out as a temporary vault file
        fixture = os.path.join(
            os.path.dirname(__file__), "fixtures/helmrepo/mysql-1.6.4.tgz"
        )
        src = (
            """
          eval:
            tempfile:
                eval:
                  file: %s
                select: contents
            encoding: vault
        """
            % fixture
        )
        vault = make_vault_lib("password")
        yaml = make_yaml(vault)
        expr = yaml.load(io.StringIO(src))
        resource = self._getTestResource()
        resource.attributeManager = AttributeManager(yaml)
        resource._templar._loader.set_vault_secrets(vault.secrets)
        pickled = pickle.dumps(resource, -1)
        restored = pickle.loads(pickled)
        assert restored and type(restored) == type(resource)

        filePath = map_value(expr, resource)
        with open(filePath, "rb") as vf:
            vaultContents = vf.read()
            assert vaultContents.startswith(b"$ANSIBLE_VAULT;")

        # decrypt the vault file, make sure it's a sensitive_bytes string that matches the original contents
        src = """
        eval:
          file:
            eval: $tempfile
          encoding: binary
        select: contents
        """
        expr = yaml.load(io.StringIO(src))
        contents = map_value(expr, RefContext(resource, trace=2, vars=dict(tempfile=filePath)))
        assert isinstance(contents, sensitive_bytes), type(contents)
        with open(fixture, "rb") as tp:
            self.assertEqual(tp.read(), contents)

    def test_external(self):
        class ExternalTest(ExternalValue):
            def __init__(self):
                super(ExternalTest, self).__init__("ExternalTest", "test")

        singleton = ExternalTest()
        set_eval_func("externaltest", lambda arg, ctx: singleton)

        ctx = RefContext(self._getTestResource())
        expr = Ref({"eval": {"externaltest": None}})

        result = expr.resolve(ctx)
        self.assertEqual(result[0], "test")

        result = expr.resolve_one(ctx)
        self.assertEqual(result, "test")

        result = expr.resolve(ctx, wantList="result")
        self.assertIs(result[0].external, singleton)

        asTemplate = '{{ {"externaltest": none } | eval }}'
        result = map_value(asTemplate, ctx)
        assert isinstance(result, str)
        self.assertEqual(result, "test")

        ctx2 = ctx.copy(wantList="result")
        result = map_value(asTemplate, ctx2)
        self.assertIs(result.external, singleton)

        result = map_value("transformed " + asTemplate, ctx2)
        self.assertEqual(result, "transformed test")

    def test_to_env(self):
        from unfurl.yamlloader import make_yaml
        src = """
          eval:
            to_env:
              FOO: 1 # get converted to string
              BAR: false # get converted to empty string
              BAZ: true # get converted to string
              NUL: null # key gets excluded
              QUU: # redacted when serialized into yaml
                eval:
                  sensitive: "passw0rd"
              SUB: "${FOO}"
          """
        yaml = make_yaml()
        expr = yaml.load(io.StringIO(src))
        ctx = RefContext(self._getTestResource())
        env = map_value(expr, ctx)
        assert env == {'FOO': '1', 'BAR': '', 'BAZ': 'true', 'QUU': 'passw0rd', "SUB": "1"}
        out=io.StringIO()
        yaml.dump(serialize_value(env), out)
        assert out.getvalue() == '''\
FOO: '1'
BAR: ''
BAZ: 'true'
QUU: <<REDACTED>>
SUB: '1'
'''

    def test_to_env_set_environ(self):
        from unfurl.yamlloader import make_yaml
        src = """
          eval:
            to_env:
              ^PATH: /foo/bin
            update_os_environ: true
          """
        yaml = make_yaml()
        expr = yaml.load(io.StringIO(src))
        ctx = RefContext(self._getTestResource())
        path = os.environ['PATH']
        env = map_value(expr, ctx)
        new_path = "/foo/bin:"+path
        assert os.environ['PATH'] == new_path
        assert env == {'PATH': new_path}

    def test_labels(self):
        import io
        from unfurl.yamlloader import make_yaml
        src = """
          eval:
            to_googlecloud_label:
              Url: https://foo-bar.com
              Missing: null
            digest: d80912dc
          """
        yaml = make_yaml()
        expr = yaml.load(io.StringIO(src))
        ctx = RefContext(self._getTestResource())
        labels = map_value(expr, ctx)
        assert labels == {'url': "https______foo-bar__comvit"}

        src = """
          eval:
            to_label: "1 convert me"
            replace: _
            max: 10
            case: upper
            start_prepend: _
            digestlen: 0  # disable digest on truncation
          """
        yaml = make_yaml()
        expr = yaml.load(io.StringIO(src))
        ctx = SafeRefContext(self._getTestResource())
        label = map_value(expr, ctx)
        assert label == "_1CON_RTME"

        src = """
          eval:
            to_label: "1 Convert Me"
            replace: _
            max: "{{ 5 + 5 }}"
            case: lower
            start_prepend: _
          """
        yaml = make_yaml()
        expr = yaml.load(io.StringIO(src))
        ctx = SafeRefContext(self._getTestResource())
        label = map_value(expr, ctx)
        assert label == "_1converrd"

        src = """
          eval:
            to_label:
            - longprefix
            - name
            - suffix
            sep: .
            max: 10
          """
        yaml = make_yaml()
        expr = yaml.load(io.StringIO(src))
        ctx = SafeRefContext(self._getTestResource())
        label = map_value(expr, ctx)
        assert len(label) == 10
        assert label == "lo.na.suRC"

        src = """
          eval:
            to_label:
            - longprefix
            - name
            - suffix
            sep: .
            replace: "-"
            max: 20
          """
        yaml = make_yaml()
        expr = yaml.load(io.StringIO(src))
        ctx = SafeRefContext(self._getTestResource())
        label = map_value(expr, ctx)
        assert len(label) == 20
        assert label == "longpr.name.suffixRC"

        src = """
          eval:
            to_label:
            - reallyreallylongprefix
            - short
            sep: .
            replace: "-"
            max: 20
          """
        yaml = make_yaml()
        expr = yaml.load(io.StringIO(src))
        ctx = SafeRefContext(self._getTestResource())
        label = map_value(expr, ctx)
        assert len(label) == 19
        assert label == "reall-refix.shortyi"

        src = """
          eval:
            to_label:
            - longprefix
            - name
            - suffix
            sep: .
            max: 1
          """
        yaml = make_yaml()
        expr = yaml.load(io.StringIO(src))
        ctx = SafeRefContext(self._getTestResource())
        label = map_value(expr, ctx)
        assert label == "R"

        # allow [...] since users might expect that to work
        src = r"""
            eval:
              to_googlecloud_label:
                eval: .name
              allowed: "[a-zA-Z0-9-]"
              replace: "--"
          """
        yaml = make_yaml()
        expr = yaml.load(io.StringIO(src))
        ctx = SafeRefContext(NodeInstance("test_nodash-yeshyphen"))
        label = map_value(expr, ctx)
        assert label == "test--nodash-yeshyphen"
        assert 'cy-cy-wordpress-lrtkxtb8-lruwqqt2-container--service' == to_dns_label(
                "cy-cy-wordpress-lrtkxtb8-lruwqqt2-container_service", max=52
        )
        assert 'x--foo--bar' == map_value(
            "{{ __unfurl.to_dns_label('_foo/_bar'.split('/')) }}",
            ctx
        )

    def test_urljoin(self):
        resource = self._getTestResource()
        # default port is omitted
        test1 = dict(eval={"urljoin": ["http", "localhost", 80]})
        result1 = Ref(test1).resolve_one(RefContext(resource))
        self.assertEqual("http://localhost", result1)

        # no port
        test2 = dict(eval={"urljoin": ["http", "localhost", "", "path"]})
        result2 = Ref(test2).resolve_one(RefContext(resource))
        self.assertEqual("http://localhost/path", result2)

        # port (string ok)
        test2 = dict(eval={"urljoin": ["http", "localhost", "8080", "path"]})
        result2 = Ref(test2).resolve_one(RefContext(resource))
        self.assertEqual("http://localhost:8080/path", result2)

        # list of at least length 2 is required but eval to None if empty
        test3 = dict(eval={"urljoin": [None, None]})
        result3 = Ref(test3).resolve_one(RefContext(resource))
        assert result3 is None

def pairs(iterable):
    i = iter(iterable)
    try:
        while True:
            yield next(i), next(i)
    except StopIteration:
        pass


def test_env_sub():
    env = dict(baz="env", alt="env2", bad=None)
    tests = [
      "${baz} ${bar:default value}", "env default value",
      "foo${baz|missing}${bar:default value}", "fooenvdefault value",
      r"foo\${baz}${bar:default value}", "foo${baz}default value",
      r"foo\\${baz}${bar:default value}", r"foo\${baz}default value",
      "${missing|baz} ${missing|missing2:default value}", "env default value",
      "${bad}", ""
    ]
    for test, expected in pairs(tests):
        assert expected == substitute_env(test, env), test

def test_analyze_expr():
    result = analyze_expr(".targets::foo::baz")
    assert result and result.get_keys() == ["$start", ".targets", "foo", "baz"]

    result = analyze_expr("$SOURCE::.targets::foo::baz", ["SOURCE"])
    assert result and result.get_keys() == ["SOURCE", ".targets", "foo", "baz"]

    result = analyze_expr({"get_property": ["HOST", "host", "disk_size"]})
    assert result and result.get_keys() == ['$start']

    result = analyze_expr({"eval":".capabilities::[.name=cap_name]::foo", "trace":0})
    assert result and result.get_keys() == ["$start", ".capabilities", "cap_name", "foo"]

    result = analyze_expr({"eval":"::node::foo", "trace":0})
    assert result and result.get_keys() == ['$start', "::node", "foo"]

    result = analyze_expr({"eval": "foo", "trace":0})
    assert result and result.get_keys() == ['$start', ".ancestors", "foo"]

    result = analyze_expr(dict(eval=".hosted_on[.type=unfurl.nodes.K8sNamespace]::foo"))
    assert result and result.get_keys() == ['$start', ".hosted_on", ".type", "unfurl.nodes.K8sNamespace", "foo"]
