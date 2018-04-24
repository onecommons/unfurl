#!/usr/bin/env python
"""
Applies a cluster specification to a cluster
"""
import sys
import os
from . import run
import click

# check for AWS access info
if os.getenv('AWS_ACCESS_KEY_ID') is None or os.getenv('AWS_SECRET_ACCESS_KEY') is None:
  print('AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY **MUST** be exported as environment variables.')
  sys.exit(1)

@click.command()
@click.argument('manifest_file', default='cluster-manifest.yaml', nargs=1, type=click.Path(exists=True))
@click.option('--resource', help="name of resource to start with")
@click.help_option('--help', '-h')
@click.option('-v', '--verbose', count=True)
def main(manifest, **options):
  run(manifest, options)

if __name__ == '__main__':
    main()
