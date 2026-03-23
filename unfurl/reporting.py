# Copyright (c) 2022 Adam Souzis
# SPDX-License-Identifier: MIT
import itertools
import json
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Sequence,
    Tuple,
    Union,
    Optional,
    TYPE_CHECKING,
    cast,
    overload,
    Mapping,
)
from typing_extensions import Literal
from .result import Results
from .runtime import EntityInstance, NodeInstance
from .planrequests import (
    PlanRequest,
    TaskRequestGroup,
    JobRequest,
)
from .support import Status
from .logs import SensitiveFilter, getLogger, getConsole
from rich.console import Console
from rich.table import Table
from rich.tree import Tree
from rich import box
from rich.segment import Segment
from rich.markup import escape
import re

if TYPE_CHECKING:
    from .yamlmanifest import YamlManifest
    from .cloudmap import CloudMapDB
    from rich.console import RenderableType
    from rich.style import StyleType
    from .job import Job, ConfigTask

logger = getLogger("unfurl")


class JobTable(Table):
    max_extra_lines = 2

    def __init__(self, **kwargs):
        super().__init__(box=box.HORIZONTALS, show_lines=True, expand=True, **kwargs)
        self.hacks = {}

    def _render(self, console: "Console", options, widths):
        new_line = Segment.line()
        _box = self.box
        table_style = console.get_style(self.style or "")
        border_style = table_style + console.get_style(self.border_style or "")
        extra = None
        # width = sum(widths)  # XXX use to center extra
        for segment in super()._render(console, options, widths):
            if not isinstance(segment, Segment):
                yield segment
                continue
            if self._match(segment.text):
                first, extra = self._match(segment.text)
                if first:  # might be empty if the text had styling
                    yield segment._replace(text=first)
            elif extra and segment.text == new_line.text:
                # add line of text that spans across all the columns
                yield segment
                if _box:
                    yield Segment(_box.mid_left, border_style)
                    text = console.render_str(extra)
                    count = 0
                    for segment_list in console.render_lines(
                        text, options.update(no_wrap=False, overflow="fold")
                    ):
                        count += 1
                        if count > self.max_extra_lines:
                            break
                        yield from segment_list
                    yield Segment(_box.mid_right, border_style)
                yield Segment.line()
                extra = None
            else:
                yield segment

    def _match(self, s):
        m = re.match(r"=(.+?)=(.*)", s)
        if m:
            return (m.group(2), self.hacks[m.group(1)])
        return None

    def add_row(
        self,
        *renderables: Optional["RenderableType"],
        style: Optional["StyleType"] = None,
        end_section: bool = False,
        extra: Optional["RenderableType"] = None,
    ) -> None:
        if extra is not None:
            hackid = str(len(self.hacks))
            hack = f"={hackid}={renderables[-1]}"
            self.hacks[hackid] = extra
        super().add_row(
            *(renderables[:-1] + (hack,)), style=style, end_section=end_section
        )


class JobReporter:
    @staticmethod
    def _job_request_summary(
        requests: List[JobRequest], manifest: Optional["YamlManifest"]
    ) -> Iterable[dict]:
        for request in requests:
            # XXX better reporting
            node = dict(instance=request.name)
            if manifest:
                node["job_request"] = manifest.path or ""
            else:
                node["job_request"] = "local"
            if request.target:
                node["status"] = str(request.target.local_status)
            yield node

    @staticmethod
    def _switch_target(
        target: NodeInstance, old_summary_list: List[dict]
    ) -> List[dict]:
        new_summary_list: List[dict] = []
        node = dict(
            instance=target.name,
            status=str(target.status),
            state=str(target.state),
            managed=target.created,
            plan=new_summary_list,
        )
        old_summary_list.append(node)
        return new_summary_list

    @staticmethod
    def _list_plan_summary(
        requests: Sequence[Union[PlanRequest, JobRequest]],
        target: Optional[NodeInstance],
        parent_summary_list: List[dict],
        include_rendered: bool,
        workflow: str,
    ) -> None:
        summary_list = parent_summary_list
        for request in requests:
            if isinstance(request, JobRequest):
                summary_list.extend(JobReporter._job_request_summary([request], None))
                continue
            if isinstance(request, TaskRequestGroup) and not request.children:
                continue  # don't include in the plan
            if request.target is not target:
                if workflow == "deploy" and not request.include_in_plan():
                    continue
                # target changed, add it to the parent's list
                # switch to the "plan" member of the new target
                target = cast(NodeInstance, request.target)
                summary_list = JobReporter._switch_target(target, parent_summary_list)
            if isinstance(request, TaskRequestGroup):
                sequence: List = []
                group: Dict[str, Any] = {}
                if request.workflow:
                    group["workflow"] = str(request.workflow)
                group["sequence"] = sequence
                summary_list.append(group)
                JobReporter._list_plan_summary(
                    request.children, target, sequence, include_rendered, workflow
                )
            else:
                if hasattr(request, "_summary_dict"):
                    summary_list.append(request._summary_dict(include_rendered))

    @staticmethod
    def json_plan_summary(
        job: "Job", pretty: bool = False, include_rendered: bool = True
    ) -> Union[str, list]:
        """
        Return a list of items that look like:

          {
          instance: target_name,
          status: target_status,
          plan: [
              {"operation": "check"
                "sequence": [
                    <items like these>
                  ]
              }
            ]
          }
        """
        summary: List[dict] = []
        if job.external_requests:
            for m, requests in job.external_requests:
                summary.extend(JobReporter._job_request_summary(requests, m))
        if job.plan_requests:
            JobReporter._list_plan_summary(
                job.plan_requests,
                None,
                summary,
                include_rendered,
                job.jobOptions.workflow,
            )
        if not pretty:
            return summary
        else:
            return json.dumps(summary, indent=2)

    @overload
    @staticmethod
    def stats(tasks, asMessage: Literal[False]) -> Dict[str, int]: ...

    @overload
    @staticmethod
    def stats(tasks) -> Dict[str, int]: ...

    @overload
    @staticmethod
    def stats(tasks, asMessage: Literal[True]) -> str: ...

    @overload
    @staticmethod
    def stats(tasks, asMessage: bool) -> Union[Dict[str, int], str]: ...

    @staticmethod
    def stats(tasks: List["ConfigTask"], asMessage=False):
        # note: the status of the task, not the target resource
        key = lambda t: (
            Status.absent
            if t.blocked
            else (
                Status.error
                if t.target_status == Status.error
                else t._localStatus or Status.unknown
            )
        )
        tasks = sorted(tasks, key=key)
        stats = dict(total=len(tasks), ok=0, error=0, unknown=0, skipped=0)
        for k, g in itertools.groupby(tasks, key):
            if not k:  # is a Status
                stats["skipped"] = len(list(g))
            elif k == Status.absent:
                stats["blocked"] = len(list(g))
            else:
                stats[k.name] = len(list(g))
        stats["changed"] = len([t for t in tasks if t.modified_target])
        if asMessage:
            return JobReporter.format_stats(stats)
        return stats

    @staticmethod
    def format_stats(stats: Dict[str, int]) -> str:
        if "blocked" not in stats:
            stats["blocked"] = 0
        return "{total} tasks ({changed} changed, {ok} ok, {error} failed, {blocked} blocked, {unknown} unknown, {skipped} skipped)".format(
            **stats
        )

    @staticmethod
    def plan_summary(
        job: "Job",
        plan_requests: List[PlanRequest],
        external_requests: Iterable[Tuple[Any, Any]],
        verbose=False,
    ) -> Tuple[str, int]:
        """
        Node "site" (status, state, created):
          check: Install.check
          workflow: # if group
            Standard.create (reason add)
            Standard.configure (reason add)
        """
        INDENT = 4
        count = 0

        def _summary(
            requests: Sequence[Union[JobRequest, PlanRequest]],
            target: Optional[EntityInstance],
            indent: int,
        ) -> None:
            nonlocal count
            for request in requests:
                if isinstance(request, TaskRequestGroup):
                    group = request
                else:
                    group = None
                if group and not group.children:
                    continue
                if isinstance(request, JobRequest):
                    count += 1
                    nodeStr = f'Job for "{request.name}":'
                    output.append(" " * indent + nodeStr)
                    continue
                if not job.is_filtered() and job.jobOptions.workflow == "deploy":
                    if not request.include_in_plan():
                        logger.trace(
                            'excluding "%s" from plan: not required',
                            request.target.template.nested_name,
                        )
                        continue
                if request.target is not target:
                    target = request.target
                    assert target
                    status = ", ".join(
                        filter(
                            None,
                            (
                                target.local_status.name
                                if target.local_status is not None
                                else "",
                                target.state.name if target.state is not None else "",
                                "managed" if target.created else "",
                            ),
                        )
                    )
                    nodeStr = f'Node "{target.template.nested_name}" ({status}):'
                    output.append(" " * indent + nodeStr)
                if group:
                    output.append(
                        "%s- %s:" % (" " * indent, (group.workflow or "sequence"))
                    )
                    _summary(group.children, target, indent + INDENT)
                else:
                    count += 1
                    output.append(" " * indent + f"- operation {request.name}")
                    if request.task:
                        if request.task._workFolders:
                            for wf in request.task._workFolders.values():
                                output.append(" " * indent + f"   rendered at {wf.cwd}")
                        if request.not_ready:
                            # don't report error if waiting
                            if request.dependencies:
                                msg = "render waiting for dependents"
                            else:
                                msg = "render deferred due to errors"
                            if verbose:
                                output.append(" " * indent + f"   {msg}:")
                                output.append(
                                    " " * indent
                                    + f"   {[d.name for d in request.get_unfulfilled_refs()]}"
                                )
                            else:
                                output.append(" " * indent + f"   ({msg})")
                        elif request.task._errors or request.render_errors:
                            output.append(" " * indent + "   (errors while rendering)")

        opts = job.jobOptions.get_user_settings()
        options = ",".join([f"{k} = {opts[k]}" for k in opts if k != "planOnly"])
        header = f"Plan for {job.jobOptions.workflow}"
        if options:
            header += f" ({options})"
        output: List[str] = [header + ":\n"]

        for m, jr in external_requests:
            if jr:
                count += 1
                output += [f" External jobs on {m.path}:"]
                for j in jr:
                    output.append(" " * INDENT + j.name)

        _summary(plan_requests, None, 0)
        if not count:
            output.append("Nothing to do.")
        return "\n".join(output), count

    @staticmethod
    def summary_table(job: "Job") -> str:
        console = getConsole(record=True)
        if not job.workDone:
            console.print(
                f"Job {job.changeId} completed: [{job.status.color}]{job.status.name}[/]. No tasks ran."
            )
            return console.export_text()

        logger.info("", extra=dict(json=job.json_summary(add_rendered=True)))
        title = "Job %s completed in %.3fs: [%s]%s[/]. %s:\n    " % (
            job.changeId,
            job.time_elapsed,
            job.status.color,
            job.status.name,
            job.stats(asMessage=True),
        )
        console.print(title)
        table = JobTable()
        table.add_column("Task", justify="right", style="cyan", no_wrap=True)
        table.add_column("Resource", style="magenta")
        table.add_column("Operation", style="magenta")
        table.add_column("Reason", style="magenta")
        table.add_column("Status", style="magenta")
        table.add_column("State", style="magenta")
        table.add_column("Changed", style="magenta")

        for i, task in enumerate(job.workDone.values()):
            if task.result:
                if task.result.success:
                    task_success = "[green]success[/]"
                elif task.blocked:
                    task_success = "[red]blocked[/]"
                else:
                    task_success = "[red]failed[/]"
            else:
                task_success = "[white]skipped[/]"
            operation = task.configSpec.operation
            reason = task.reason or ""
            resource = task.target.nested_name
            if task.target_status is None:
                status = ""
            else:
                target_status = task.target.status
                if target_status != task.target_status:
                    status = f"[{task.target_status.color}]{task.target_status.name}[/]/[{target_status.color}]{target_status.name}[/]"
                else:
                    status = f"[{task.target_status.color}]{task.target_status.name.upper()}[/]"
            state = (task.target_state and task.target_state.name) or ""
            changed = "[green]Yes[/]" if task.modified_target else "[white]No[/]"
            if task.result and task.result.result:
                output = task.result.result
                if isinstance(output, Mapping):
                    # sort dict so that the longest values are last if a string, list, or dict otherwise preserve key order
                    if output.get("msg"):
                        output = output["msg"]
                    else:
                        output = {
                            k: v.map_all() if isinstance(v, Results) else v
                            for i, (k, v) in sorted(
                                enumerate(output.items()),
                                key=lambda x: (
                                    len(x[1][1])
                                    if isinstance(x[1][1], (str, list, dict))
                                    else x[0]
                                ),
                            )
                        }
                result = escape(f"Output: {SensitiveFilter.redact(output)}")
            else:
                result = ""
            table.add_row(
                f"{i + 1} ({task_success})",
                resource,
                operation,
                reason,
                status,
                state,
                changed,
                extra=result,
            )
        console.print(table)
        return console.export_text()


def print_cloudmap_graph(db: "CloudMapDB", start_url: str = "") -> None:
    """Print a rich.Tree showing how cloudmap records reference each other."""
    from .cloudmap import CloudMapDB, Repository, CloudType, Service
    from .oci import Artifact, Instantiation

    _KIND_STYLES: Dict[str, str] = {
        "Repository": "green",
        "Artifact": "cyan",
        "Instantiation": "yellow",
        "Service": "magenta",
        "Type": "blue",
    }

    def _find_record(url: str) -> Optional[Tuple[str, Any]]:
        for kind, collection in (
            ("Service", db.services),
            ("Instantiation", db.instantiations),
            ("Artifact", db.artifacts),
            ("Repository", db.repositories),
            ("Type", db.types),
        ):
            if url in collection:
                return kind, collection[url]
        # Try adding .git suffix for git:// URLs missing from repositories
        if url.startswith("git:") and not url.endswith(".git") and "#" not in url:
            git_url = url + ".git"
            if git_url in db.repositories:
                return "Repository", db.repositories[git_url]
        return None

    def _find_all_records(url: str) -> List[Tuple[str, Any]]:
        """Find all records matching a URL across all collections."""
        results: List[Tuple[str, Any]] = []
        for kind, collection in (
            ("Instantiation", db.instantiations),
            ("Repository", db.repositories),
            ("Artifact", db.artifacts),
            ("Service", db.services),
            ("Type", db.types),
        ):
            if url in collection:
                results.append((kind, collection[url]))
        return results

    def _label(kind: str, url: str, record: Any, guide_style: str = "") -> str:
        from .oci import TypeRefs

        title = ""
        if hasattr(record, "metadata") and hasattr(record.metadata, "title"):
            title = record.metadata.title or ""
        elif hasattr(record, "name"):
            title = record.name
        prefix = f"{title} " if title and title != url else ""
        # Show the record's type for Artifact, Instantiation, Service
        type_str = ""
        if hasattr(record, "type") and isinstance(record.type, TypeRefs):
            names = record.type.names()
            if names or prefix:
                # Pad so type aligns with URL (after "Kind ")
                pad = " " * len(kind)
                styled_pipe = f"[{guide_style}]\u2502[/]" if guide_style else "\u2502"
                type_str = (
                    f"\n{styled_pipe}{pad}{escape(prefix)}({escape(', '.join(names))})"
                )
        return f"[bold]{kind}[/] {escape(url)}{type_str}"

    def _add_typed_urls(
        parent: Tree, label: str, typed_urls: Dict[str, Any], visited: set
    ) -> None:
        """Add a TypedUrls edge group: key is a label, value has type refs to follow."""
        from .oci import TypeRefs

        if not typed_urls:
            return
        branch = parent.add(f"[dim]{label}[/]")
        for key, type_refs in typed_urls.items():
            names: List[str] = []
            if isinstance(type_refs, TypeRefs):
                names = type_refs.names()
            # If the key itself is a resolvable record, follow it
            if _find_record(key) is not None:
                _add_child(branch, key, visited)
                for name in names:
                    if name != key:
                        _add_child(branch, name, visited)
            elif names:
                # Key is a label (e.g. dependency name), show it with its types
                key_node = branch.add(f"[dim italic]{escape(key)}[/]")
                for name in names:
                    _add_child(key_node, name, visited)
            else:
                _add_child(branch, key, visited)

    def _add_edges(parent: Tree, record: Any, kind: str, visited: set) -> None:
        edges: List[Tuple[str, List[str]]] = []

        if kind == "Repository":
            assert isinstance(record, Repository)
            if record.fork_of:
                edges.append(("fork_of", [record.fork_of]))
            if record.mirror_of:
                edges.append(("mirror_of", [record.mirror_of]))
            if record.service:
                edges.append(("service", [record.service]))
            if record.notable:
                urls = [
                    nd.get("artifact", "")
                    for nd in record.notable.values()
                    if isinstance(nd, dict) and nd.get("artifact")
                ]
                if urls:
                    edges.append(("notable", urls))

        elif kind == "Artifact":
            assert isinstance(record, Artifact)
            if record.notable:
                _add_typed_urls(parent, "notable", record.notable, visited)
            if record.dependencies:
                _add_typed_urls(parent, "dependencies", record.dependencies, visited)
            if record.instantiates and record.instantiates.names():
                edges.append(("instantiates", record.instantiates.names()))
            if record.instantiated_by:
                edges.append(("instantiated_by", list(record.instantiated_by)))

        elif kind == "Instantiation":
            assert isinstance(record, Instantiation)
            if record.source:
                # Render source first before TypedUrls fields
                source_branch = parent.add("[dim]source[/]")
                _add_child(source_branch, record.source, visited)
            if record.instantiated:
                _add_typed_urls(parent, "instantiated", record.instantiated, visited)
            if record.inputs:
                _add_typed_urls(parent, "inputs", record.inputs, visited)

        elif kind == "Service":
            assert isinstance(record, Service)
            if record.connections:
                _add_typed_urls(parent, "connections", record.connections, visited)
            if record.instantiated_by:
                edges.append(("instantiated_by", list(record.instantiated_by)))

        elif kind == "Type":
            assert isinstance(record, CloudType)
            if record.extends:
                # Omit self-references, show only first parent
                filtered = [n for n in record.extends if n != record.name]
                if filtered:
                    edges.append(("extends", filtered[:1]))
            if record.source:
                edges.append(("source", [record.source]))
            if record.model:
                edges.append(("model", [record.model]))
            if record.implementations:
                edges.append(("implementations", list(record.implementations)))

        for label, urls in edges:
            if not urls:
                continue
            branch = parent.add(f"[dim]{label}[/]")
            for url in urls:
                _add_child(branch, url, visited)

    def _add_child(parent: Tree, url: str, visited: set) -> None:
        found = _find_record(url)
        if found is None:
            if "://" in url or url.startswith("pkg:"):
                parent.add(f"[dim italic]{escape(url)} \\[missing][/]")
            else:
                parent.add(f"[dim italic]{escape(url)}[/]")
            return
        child_kind, child_record = found
        style = _KIND_STYLES.get(child_kind, "")
        if url in visited:
            parent.add(f"{_label(child_kind, url, child_record, style)} [dim]\\[seen][/]")
            return
        visited.add(url)
        child_tree = parent.add(
            _label(child_kind, url, child_record, style), guide_style=style
        )
        _add_edges(child_tree, child_record, child_kind, visited)

    console = getConsole()

    if start_url:
        all_found = _find_all_records(start_url)
        if not all_found:
            console.print(f"[red]Record not found:[/] {escape(start_url)}")
            return
        visited: set = {start_url}
        for kind, record in all_found:
            style = _KIND_STYLES.get(kind, "")
            root = Tree(_label(kind, start_url, record, style), guide_style=style)
            _add_edges(root, record, kind, visited)
            console.print(root)
    else:
        root = Tree("[bold]CloudMap[/]")
        visited = set()
        for section_name, collection in (
            ("Repositories", db.repositories),
            ("Artifacts", db.artifacts),
            ("Instantiations", db.instantiations),
            ("Services", db.services),
            ("Types", db.types),
        ):
            if not collection:
                continue
            section = root.add(f"[bold]{section_name}[/]")
            for url, record in collection.items():
                cls_name = record.__class__.__name__
                kind = "Type" if cls_name == "CloudType" else cls_name
                style = _KIND_STYLES.get(kind, "")
                if url in visited:
                    section.add(
                        f"{_label(kind, url, record, style)} [dim]\\[seen][/]"
                    )
                    continue
                visited.add(url)
                node = section.add(
                    _label(kind, url, record, style), guide_style=style
                )
                _add_edges(node, record, kind, visited)
        console.print(root)
