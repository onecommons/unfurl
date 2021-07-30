import logging
import os
from collections import MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from octodns.manager import Manager
from ruamel.yaml import YAML

from unfurl.configurator import Configurator
from unfurl.job import ConfigTask
from unfurl.projectpaths import WorkFolder

log = logging.getLogger(__file__)

OPERATION = None  # hack for unit tests


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


@dataclass
class DnsProperties:
    """unfurl.nodes.DNSZone properties"""

    name: str
    """DNS name of the zone"""
    exclusive: bool
    """Remove records from the zone not specified in `records`"""
    provider: dict
    """OctoDNS provider configuration"""
    records: dict
    """DNS records to add to the zone"""


class OctoDnsConfigurator(Configurator):
    """Configurator for managing DNS records with OctoDNS"""

    def can_dry_run(self, task):
        return True

    def render(self, task: ConfigTask):
        """Create yaml config files which will be consumed by OctoDNS"""
        task.logger.debug("OctoDNS configurator - rendering config files")
        folder = task.set_work_folder()
        path = folder.real_path()
        properties = self.extract_properties_from(task)
        self._create_main_config_file(folder, properties)
        records = None
        op = OPERATION or task.configSpec.operation
        if op == "configure":
            records = self.render_configure(path, properties)
        elif op == "delete":
            records = {properties.name: {}}
        elif op == "check":
            self._dump_current_dns_records(path, properties.name)
        else:
            raise NotImplementedError(f"Operation '{op}' is not allowed")

        if records:
            self._create_zone_files(folder, records)
            task.target.attributes["zone"] = records
        return records

    def extract_properties_from(self, task) -> DnsProperties:
        name = task.vars["SELF"]["name"]
        exclusive = task.vars["SELF"]["exclusive"]
        provider = task.vars["SELF"]["provider"]
        provider.resolve_all()
        provider = provider.serialize_resolved()
        desired_zone_records = task.vars["SELF"]["records"]
        desired_zone_records.resolve_all()
        desired_zone_records = {name: desired_zone_records.serialize_resolved()}
        return DnsProperties(name, exclusive, provider, desired_zone_records)

    def render_configure(self, path: str, properties: DnsProperties):
        if properties.exclusive:
            return properties.records
        self._dump_current_dns_records(path, properties.name)
        current_zone_records = self._read_current_dns_records(path, properties.name)
        return self._merge_dns_records(properties.records, current_zone_records)

    def _create_main_config_file(self, folder: WorkFolder, properties: DnsProperties):
        content = {
            "providers": {
                "source_config": {
                    "class": "octodns.provider.yaml.YamlProvider",
                    "directory": "./",
                },
                "target_config": properties.provider,
            },
            "zones": {
                properties.name: {
                    "sources": ["source_config"],
                    "targets": ["target_config"],
                }
            },
        }
        folder.write_file(content, "dns/main-config.yaml")

    def _dump_current_dns_records(self, path: str, zone_name: str):
        log.debug("OctoDNS configurator - downloading current DNS records")

        with change_cwd(path):
            try:
                manager = Manager(config_file="dns/main-config.yaml")
                manager.dump(
                    zone_name,
                    output_dir=f"{path}dns-dump/",
                    lenient=False,
                    split=False,
                    source="target_config",
                )
            except Exception as e:
                log.error("OctoDNS error: %s", e)

    def _read_current_dns_records(self, path: str, zone: str) -> dict:
        records = {}
        path = Path(path) / "dns-dump" / f"{zone}yaml"
        if path.exists():
            with open(path) as f:
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

    def run(self, task: ConfigTask):
        """Apply DNS configuration"""
        op = OPERATION or task.configSpec.operation
        task.logger.debug(f"OctoDNS configurator - run - {op}")
        if op == "configure":
            yield self.run_configure(task)
        elif op == "delete":
            yield self.run_delete(task)
        elif op == "check":
            yield self.run_check(task)
        else:
            raise NotImplementedError(f"Operation '{op}' is not allowed")

    def run_configure(self, task: ConfigTask):
        """Create or update zone"""
        work_folder = task.set_work_folder()
        with change_cwd(f"{work_folder.cwd}/dns"):
            try:
                manager = Manager(config_file="main-config.yaml")
                manager.sync(dry_run=task.dry_run)
                return task.done(success=True, result={"msg": "OctoDNS synced"})
            except Exception as e:
                log.error("OctoDNS error: %s", e)
                return task.done(success=False, result={"msg": f"OctoDNS error: {e}"})

    @staticmethod
    def run_delete(task: ConfigTask):
        """Remove zone records.

        Creates an empty configuration and apply it
        """
        work_folder = task.set_work_folder()
        with change_cwd(f"{work_folder.cwd}/dns"):
            try:
                manager = Manager(config_file="main-config.yaml")
                manager.sync(dry_run=task.dry_run)
                return task.done(success=True, result={"msg": "OctoDNS synced"})
            except Exception as e:
                log.error("OctoDNS error: %s", e)
                return task.done(success=False, result={"msg": f"OctoDNS error: {e}"})

    def run_check(self, task: ConfigTask):
        """Retrieves current zone data, compares with expected, updates status
        dump current configuration
        compore with expected
        """
        work_folder = task.set_work_folder()
        properties = self.extract_properties_from(task)
        current_records = self._read_current_dns_records(
            work_folder.cwd, properties.name
        )

        if current_records == properties.records:
            return task.done(success=True, result={"msg": "DNS records in sync"})
        else:
            return task.done(success=False, result={"msg": "DNS records out of sync"})
