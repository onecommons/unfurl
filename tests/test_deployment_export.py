import pprint
import pytest
import json
import os.path
from unfurl.yamlmanifest import YamlManifest
from unfurl.util import UnfurlError
from unfurl.to_json import to_deployment
from unfurl.localenv import LocalEnv
from unfurl.planrequests import _find_implementation
from pathlib import Path


#dynamically called
def assertions_nestedcloud(repositories, **kwargs):
    # asserting the repositories key exists
    pass


@pytest.mark.parametrize(
    "ensemble",
    (pytest.param(param) for param in Path(Path(__file__).resolve().parent, 'fixtures/ensembles').glob('*.yaml') if param.stem != "unfurl")
)
def test_ensemble(ensemble):
    test_name = ensemble.stem

    local = LocalEnv(ensemble)
    jsonExport = to_deployment(local)
    
    print(jsonExport.keys())

    globals()[f"assertions_{test_name}"](**jsonExport)
