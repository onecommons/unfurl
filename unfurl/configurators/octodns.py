import logging
import os
from collections import MutableMapping
from contextlib import contextmanager

from octodns.manager import Manager
from ruamel.yaml import YAML

from unfurl.configurator import Configurator
from unfurl.projectpaths import WorkFolder

YAML_PROVIDER = "config"
log = logging.getLogger(__file__)


def yaml_to_dict(content: str) -> dict:
    """Converts content from yaml to python dict

    The content can be either yaml in form of Ansible string or ruamel.YAML data type.
    Here we first convert it to str and then try to load it as a yaml.
    """
    yaml = YAML(typ="safe")
    return yaml.load(str(content))


@contextmanager
def change_cwd(new_path: str):
    """Temporally change current working directory"""
    log.debug("Changing CWD to: %s", new_path)
    old_path = os.getcwd()
    os.chdir(new_path)
    yield
    log.debug("Changing CWD to: %s", new_path)
    os.chdir(old_path)


def dict_merge(d1, d2):
    """Update two dicts of dicts recursively, if either mapping has leaves that are non-dicts,
    the second's leaf overwrites the first's.

    https://stackoverflow.com/questions/7204805/how-to-merge-dictionaries-of-dictionaries/24088493#24088493
    """
    for k, v in d1.items():
        if k in d2:
            if all(isinstance(e, MutableMapping) for e in (v, d2[k])):
                d2[k] = dict_merge(v, d2[k])
    d3 = d1.copy()
    d3.update(d2)
    return d3


class OctoDnsConfigurator(Configurator):
    """Configurator for managing DNS records with OctoDNS"""

    def render(self, task):
        """Create yaml config files which will be consumed by OctoDNS"""
        task.logger.debug("OctoDNS configurator - rendering config files")
        folder = task.set_work_folder()
        path = folder.real_path()
        main_config = yaml_to_dict(task.inputs["main-config"])
        desired_zone_records = {
            k: yaml_to_dict(zone) for k, zone in task.inputs["zones"].items()
        }
        self._create_main_config_file(folder, main_config)
        self._dump_current_dns_records(path, main_config)
        current_zone_records = self._read_current_dns_records(
            path, main_config["zones"]
        )
        records = self._merge_dns_records(desired_zone_records, current_zone_records)
        self._create_zone_files(folder, records)

        task.target.attributes["dns_zones"] = records
        return records

    def _create_main_config_file(self, folder: WorkFolder, content: dict):
        folder.write_file(content, "dns/main-config.yaml")

    def _dump_current_dns_records(self, path: str, config: dict):
        log.debug("OctoDNS configurator - downloading current DNS records")
        providers = filter(lambda p: p != YAML_PROVIDER, config["providers"])

        with change_cwd(path):
            try:
                manager = Manager(config_file="dns/main-config.yaml")
                for provider in providers:
                    for zone in config["zones"]:
                        manager.dump(
                            zone,
                            output_dir=f"{path}dns-dump/",
                            lenient=False,
                            split=False,
                            source=provider,
                        )
            except Exception as e:
                log.error("OctoDNS error: %s", e)

    def _read_current_dns_records(self, path: str, zones: dict) -> dict:
        records = {}
        for zone in zones:
            with open(f"{path}dns-dump/{zone}yaml") as f:
                records[zone] = yaml_to_dict(f.read())
        return records

    def _merge_dns_records(
        self, new_zone_records: dict, old_zone_records: dict
    ) -> dict:
        return dict_merge(old_zone_records, new_zone_records)

    def _create_zone_files(self, folder: WorkFolder, records: dict):
        for zone, content in records.items():
            folder.write_file(content, f"dns/{zone}yaml")

    def run(self, task):
        """Apply DNS configuration"""
        task.logger.debug("OctoDNS configurator - run")
        work_folder = task.set_work_folder()
        with change_cwd(f"{work_folder.cwd}/dns"):
            try:
                manager = Manager(config_file="main-config.yaml")
                manager.sync(dry_run=task.dry_run)
                yield task.done(success=True, result="OctoDNS synced")
            except Exception as e:
                log.error("OctoDNS error: %s", e)
                yield task.done(success=False, result=f"OctoDNS error: {e}")

    def can_dry_run(self, task):
        return True
