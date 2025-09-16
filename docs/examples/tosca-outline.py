"""An illustrative TOSCA service template"""

import tosca
from . import my_shared_types as base

tosca_community_contributions = tosca.Repository(
    name="tosca-community-contributions",
    url="https://github.com/oasis-open/tosca-community-contributions.git",
)
# repository must be defined before importing from it
from tosca_repositories.tosca_community_contributions.profiles.orchestration.profile import *

tosca_metadata = {
    "template_name": "hello world",
    "template_author": "onecommons",
    "template_version": "1.0.0",
}
