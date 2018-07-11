#!/usr/bin/env python
"""
Applies a GitErOp manifest

For each configuration checks if it should be run, records the result
"""
import sys
import os
from . import run
import click

@click.command()
@click.argument('manifest_file', default='cluster-manifest.yaml', nargs=1, type=click.Path(exists=True))
@click.option('--resource', help="name of resource to start with")
@click.help_option('--help', '-h')
@click.option('-v', '--verbose', count=True)
def main(manifest, **options):
  print(run(manifest, options).summary())

if __name__ == '__main__':
  main()
