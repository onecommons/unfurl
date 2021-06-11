"""A setuptools based setup module.
"""

# Always prefer setuptools over distutils
from setuptools import setup


setup(
    setup_requires=["pbr"],
    pbr=True,
    # XXX don't include .py files that haven't been committed to git
    include_package_data=True,
)
