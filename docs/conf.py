# Configuration file for the Sphinx documentation builder.
#
import sys, os

sys.path.insert(0, os.path.abspath(".."))
import unfurl

VERSION = unfurl.__version__


# -- Project information -----------------------------------------------------

project = "Unfurl"
copyright = "2019, Adam Souzis"
author = "Adam Souzis"
release = VERSION

# -- General configuration ---------------------------------------------------

# Add any Sphinx extension module names here, as strings. They can be
# extensions coming with Sphinx (named 'sphinx.ext.*') or your custom
# ones.
extensions = [
    "recommonmark",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.githubpages",
    "sphinx_click.ext",
    "sphinx-jsonschema",
]

# Add any paths that contain templates here, relative to this directory.
templates_path = ["_templates"]

# List of patterns, relative to source directory, that match files and
# directories to ignore when looking for source files.
# This pattern also affects html_static_path and html_extra_path.
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]


# -- Options for HTML output -------------------------------------------------
# see https://github.com/myyasuda/sphinxbootstrap4theme#html-theme-options

# The theme to use for HTML and HTML Help pages.  See the documentation for
# a list of builtin themes.
html_theme_options = {
    # "show_sidebar": True,
    # "navbar_bg_class": 'faded',
    # "navbar_color_class": 'light',
    # "sidebar_fixed": False
}

# default theme:
# html_theme = "alabaster" 

import sphinxbootstrap4theme
html_theme = "sphinxbootstrap4theme"

html_theme_path = [sphinxbootstrap4theme.get_path()]

# Add any paths that contain custom static files (such as style sheets) here,
# relative to this directory. They are copied after the builtin static files,
# so a file named "default.css" will overwrite the builtin "default.css".
html_static_path = ['_static']
html_logo = "./unfurl_logo_light.svg"
