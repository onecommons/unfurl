import logging
import os
from collections import MutableMapping
from contextlib import contextmanager

from octodns.manager import Manager
from ruamel.yaml import YAML

from unfurl.configurator import Configurator
from unfurl.projectpaths import WorkFolder

log = logging.getLogger(__file__)


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
        task.inputs["main-config"].resolve_all()
        task.inputs["zones"].resolve_all()
        main_config = task.inputs["main-config"].serialize_resolved()
        desired_zone_records = task.inputs["zones"].serialize_resolved()
        self._create_main_config_file(folder, main_config)
        if task.inputs["dump_providers"]:
            self._dump_current_dns_records(
                path, main_config, task.inputs["dump_providers"]
            )
            current_zone_records = self._read_current_dns_records(
                path, main_config["zones"]
            )
        else:
            current_zone_records = {}
        records = self._merge_dns_records(desired_zone_records, current_zone_records)
        self._create_zone_files(folder, records)

        task.target.attributes["dns_zones"] = records
        return records

    def _create_main_config_file(self, folder: WorkFolder, content: dict):
        folder.write_file(content, "dns/main-config.yaml")

    def _dump_current_dns_records(self, path: str, config: dict, providers: list):
        log.debug("OctoDNS configurator - downloading current DNS records")

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
                yaml = YAML(typ="safe")
                records[zone] = yaml.load(f.read())
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
