"""A setuptools based setup module.

See:
https://packaging.python.org/en/latest/distributing.html
https://github.com/pypa/sampleproject
"""

# Always prefer setuptools over distutils
from setuptools import setup

setup(
    setup_requires=['pbr'],
    pbr=True,
    # XXX don't include .py not committed to git
    include_package_data=True
)
