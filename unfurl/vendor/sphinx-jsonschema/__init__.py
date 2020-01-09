# -*- coding: utf-8 -*-

"""
    sphinx-jsonschema
    -----------------

    This package adds the *jsonschema* directive to Sphinx.

    Using this directory you can render JSON Schema directly
    in Sphinx.

    :copyright: Copyright 2017, Leo Noordergraaf
    :licence: GPL v3, see LICENCE for details.
"""
# modifications to https://github.com/lnoor/sphinx-jsonschema/commit/cdd5ea14ed2bc66bcc9bc8af593797b4676bd248
# - note external files as build dependency

import os.path
import json
from jsonpointer import resolve_pointer
import yaml
from collections import OrderedDict

from docutils.parsers.rst import Directive
from .wide_format import WideFormat

# TODO find out if app is accessible in some other way
_glob_app = None

class JsonSchema(Directive):
    optional_arguments = 1
    has_content = True

    def __init__(self, directive, arguments, options, content, lineno, content_offset, block_text, state, state_machine):
        assert directive == 'jsonschema'

        self.options = options
        self.state = state
        self.lineno = lineno
        self.statemachine = state_machine

        if len(arguments) == 1:
            filename, pointer = self._splitpointer(arguments[0])
            if filename != '':
                self._load_external(filename)
            else:
                self._load_internal(content)
            if pointer:
                self.schema = resolve_pointer(self.schema, pointer)
        else:
            self._load_internal(content)

    def run(self):
        format = WideFormat(self.state, self.lineno, _glob_app)
        return format.transform(self.schema)

    def _load_external(self, file_or_url):
        if file_or_url.startswith('http'):
            try:
                import requests
            except ImportError:
                self.error("JSONSCHEMA loading from http requires requests. Try 'pip install requests'")
            text = requests.get(file_or_url)
            self.schema = self.ordered_load(text.content)
        else:
            if not os.path.isabs(file_or_url):
                # file relative to the path of the current rst file
                dname = os.path.dirname(self.statemachine.input_lines.source(0))
                file_or_url = os.path.join(dname, file_or_url)
            with open(file_or_url) as file:
                self.schema = self.ordered_load(file, yaml.SafeLoader)
            self.state.document.settings.env.note_dependency(file_or_url)

    def _load_internal(self, text):
        if text is None or len(text) == 0:
            self.error("JSONSCHEMA requires either filename, http url or inline content")
        self.schema = self.ordered_load('\n'.join(text), yaml.SafeLoader)

    def _splitpointer(self, path):
        val = path.split('#', 1)
        if len(val) == 1:
            val.append(None)
        return val


    def ordered_load(self, stream, Loader=yaml.Loader, object_pairs_hook=OrderedDict):
        """Allows you to use `pyyaml` to load as OrderedDict.

        Taken from https://stackoverflow.com/a/21912744/1927102
        """
        class OrderedLoader(Loader):
            pass

        def construct_mapping(loader, node):
            loader.flatten_mapping(node)
            return object_pairs_hook(loader.construct_pairs(node))
        OrderedLoader.add_constructor(
            yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
            construct_mapping)
        try:
            try:
                result = yaml.load(stream, OrderedLoader)
            except yaml.scanner.ScannerError:
                if type(stream) == str:
                    result = json.loads(stream, object_pairs_hook=object_pairs_hook)
                else:
                    stream.seek(0)
                    result = json.load(stream, object_pairs_hook=object_pairs_hook)
        except Exception as e:
            self.error(e)
            result = {}
        return result


def setup(app):
    global _glob_app
    _glob_app = app
    app.add_directive('jsonschema', JsonSchema)
    return {'version': '1.8'}
