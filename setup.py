"""A setuptools based setup module.
"""

# Always prefer setuptools over distutils
from setuptools import setup

with open("full-requirements.txt") as f:
    full_requirements = filter(
        None, [line.partition("#")[0].strip() for line in f.read().splitlines()]
    )

setup(
    setup_requires=["pbr"],
    pbr=True,
    # XXX don't include .py files that haven't been committed to git
    include_package_data=True,
    extras_require={"full": full_requirements},
)
