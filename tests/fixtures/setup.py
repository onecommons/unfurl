from setuptools import setup

# this is a fake unfurl intended to speed up install tests in tests/test_cli.py
setup(
    name="unfurl",
    version="0",
    url="https://github.com/onecommons/unfurl",
    entry_points={
        "console_scripts": [
            "unfurl = unfurl.__main__:main",
        ],
    },
)
