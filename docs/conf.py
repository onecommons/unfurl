# Configuration file for the Sphinx documentation builder.
#
import sys, os

sys.path.insert(0, os.path.abspath(".."))
import unfurl

# -- Project information -----------------------------------------------------

project = "Unfurl"
copyright = "2024, OneCommons Co."
author = "Adam Souzis"
release = unfurl.semver_prerelease()

# -- General configuration ---------------------------------------------------

# Add any Sphinx extension module names here, as strings. They can be
# extensions coming with Sphinx (named 'sphinx.ext.*') or your custom
# ones.
extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.napoleon",
    "sphinx.ext.githubpages",
    "sphinx_click.ext",
    "sphinx-jsonschema",
    "sphinx.ext.autodoc.typehints",
    "sphinxcontrib.documentedlist",
    "sphinx.ext.autosectionlabel",
    "sphinx.ext.extlinks",
    "sphinx_rtd_theme",
    "sphinx_design",
]

suppress_warnings = ["autosectionlabel.*", "toc.excluded"]

autodoc_typehints = "description"
modindex_common_prefix = ["unfurl."]

myst_enable_extensions = [
    "colon_fence",
]

# :unfurl_site:`title <page>` or :unfurl_site:`page`
extlinks = {
    "onecommons": ("https://onecommons.org/%s", None),
    "unfurl_site": ("https://unfurl.run/%s", None),
    "unfurl_github_tree": ("https://github.com/onecommons/unfurl/tree/main/", None),
    "unfurl_github_file": ("https://github.com/onecommons/unfurl/blob/main/", None),
    "tosca_spec": (
        "_static/TOSCA-Simple-Profile-YAML-v1.3-os-toc.html#%s",
        "TOSCA 1.3 Specification: %",
    ),
    "tosca_spec2": (
        "../_static/TOSCA-Simple-Profile-YAML-v1.3-os-toc.html#%s",
        "TOSCA 1.3 Specification: %s",
    ),
    "cli": (
        "cli.html#%s",
        "%s",
    ),
}

# usage: |stdlib|_
rst_epilog = """
.. |stdlib| replace:: Unfurl Cloud Standard TOSCA library
.. _stdlib: https://unfurl.cloud/onecommons/std
.. _Unfurl Cloud: https://unfurl.cloud/
"""

# Add any paths that contain templates here, relative to this directory.
templates_path = ["_templates"]

default_role = "any"

# List of patterns, relative to source directory, that match files and
# directories to ignore when looking for source files.
# This pattern also affects html_static_path and html_extra_path.
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store", "todo"]


# -- Options for HTML output -------------------------------------------------
# The theme to use for HTML and HTML Help pages.  See the documentation for
# a list of builtin themes.
html_theme_options = {
    "logo_only": True,
}

import sphinx_rtd_theme

html_theme = "sphinx_rtd_theme"

# Add any paths that contain custom static files (such as style sheets) here,
# relative to this directory. They are copied after the builtin static files,
# so a file named "default.css" will overwrite the builtin "default.css".
html_static_path = ["_static"]
html_css_files = ["custom.css"]
html_js_files = ["custom.js"]
html_logo = "./unfurl_logo.svg"
html_favicon = "favicon32.png"

# default: {"**":['globaltoc.html', 'sourcelink.html', 'searchbox.html'],
html_sidebars = {"**": ["globaltoc.html"], "index": []}
html_title = "Unfurl Documentation"
