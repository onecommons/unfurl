"""A setuptools based setup module.

See:
https://packaging.python.org/en/latest/distributing.html
https://github.com/pypa/sampleproject
"""

# Always prefer setuptools over distutils
from setuptools import setup, find_packages

setup(
    setup_requires=['pbr'],
    pbr=True,
    # XXX don't include .py not committed to git
    include_package_data=True,

    # To provide executable scripts, use entry points in preference to the
    # "scripts" keyword. Entry points provide cross-platform support and allow
    # `pip` to create the appropriate form of executable for the target
    # platform.
    #
    entry_points={  # Optional
        'console_scripts': [
            'giterop=giterop.__main__:cli',
        ],
    },
)
