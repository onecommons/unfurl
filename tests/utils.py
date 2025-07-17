from pathlib import Path
import time
import unittest
import urllib.request
from unfurl.testing import *


class MotoTest(unittest.TestCase):
    PROJECT_CONFIG = """\
      apiVersion: unfurl/v1alpha1
      kind: Project
      environments:
        defaults:
          connections:
            # declare the primary_provider as a connection to an Amazon Web Services account:
            primary_provider:
              type: unfurl.relationships.ConnectsTo.AWSAccount
              properties:
                  AWS_DEFAULT_REGION: us-east-1
                  endpoints:
                      ec2: http://localhost:5001
                      sts: http://localhost:5001
"""

    def setUp(self):
        from multiprocessing import Process

        from moto.server import main

        self.p = Process(target=main, args=(["-p5001"],))
        self.p.start()

        for n in range(5):
            time.sleep(0.2)
            try:
                url = "http://localhost:5001/moto-api"  # UI lives here
                urllib.request.urlopen(url)
            except Exception as e:  # URLError
                print(e)
            else:
                return True
        return False

    def tearDown(self):
        try:
            self.p.terminate()
        except:
            pass

def print_config(dir, homedir=None):
    if homedir:
        print("!home")
        with open(Path(homedir) / "unfurl.yaml") as f:
            print(f.read())

        print("!home/local")
        local = Path(homedir) / "local" / "unfurl.yaml"
        if local.exists():
            with open(local) as f:
                print(f.read())
        else:
            print(local, "does not exist")

    print(f"!{dir}!")
    with open(Path(dir) / "unfurl.yaml") as f:
        print(f.read())
    print(f"!{dir}/local!")
    local = Path(dir) / "local" / "unfurl.yaml"
    if local.exists():
        with open(local) as f:
            print(f.read())
    else:
        print(local, "does not exist")
