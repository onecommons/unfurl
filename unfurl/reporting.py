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

from .cloudmap import Repository, CloudType, Service
from .oci import Artifact, Instantiation, TypeRefs

if TYPE_CHECKING:
    from .yamlmanifest import YamlManifest
    from .cloudmap import CloudMapDB
    from rich.console import RenderableType
    from rich.style import StyleType
    from .job import Job, ConfigTask

CloudMapRecord = Union[
    "Repository", "Artifact", "Instantiation", "Service", "CloudType"
]

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


class CloudMapGraphVisitor:
    """Visitor interface for traversing a CloudMap graph.

    Override methods to customize how records and edges are rendered or processed.
    """

    def start_graph(self, title: str) -> None:
        """Called once at the beginning of a full graph traversal."""

    def end_graph(self, empty: bool) -> None:
        """Called once at the end of a full graph traversal."""

    def start_section(self, name: str) -> None:
        """Called at the start of each collection section (Repositories, Artifacts, etc.)."""

    def visit_record(
        self,
        kind: str,
        url: str,
        record: "CloudMapRecord",
        *,
        seen: bool = False,
        type_refs: Optional[List[Tuple[str, Optional[Dict[str, Any]]]]] = None,
        walk_only: bool = False,
    ) -> None:
        """Called for each record node.

        Args:
            type_refs: Type reference pairs ``(name, constraints)`` from a typed-URL
                       entry whose key resolved to this record.
            walk_only: When True the record is being walked only so its edges
                       are discovered; a ``visit_type_ref`` has already been
                       emitted for it and no additional ref should be added.
        """

    def leave_record(self, kind: str, url: str, record: "CloudMapRecord") -> None:
        """Called after all edges of a record have been visited."""

    def visit_relationship(self, label: str) -> None:
        """Called when entering an edge group (e.g. 'fork_of', 'notable')."""

    def leave_relationship(self, label: str) -> None:
        """Called when leaving an edge group."""

    def visit_label(
        self,
        label: str,
    ) -> None:
        """Called for a URL reference that is not resolved as a child record."""

    def visit_ref(
        self,
        url: str,
        *,
        missing: bool = False,
        type_refs: Optional[List[Tuple[str, Optional[Dict[str, Any]]]]] = None,
    ) -> None:
        """Called for a URL reference that is not resolved as a child record."""

    def visit_type_ref(
        self,
        name: str,
        constraints: Optional[Dict[str, Any]] = None,
        *,
        label: str = "",
    ) -> None:
        """Called for a type reference."""

    def leave_type_ref(self) -> None:
        """Called after a visit_type_ref and its associated record walk."""

    def not_found(self, url: str) -> None:
        """Called when a start_url is not found in the db."""


class CloudMapGraphWalker:
    """Walk a CloudMap graph calling visitor methods for each record and edge."""

    def __init__(
        self,
        db: "CloudMapDB",
        visitor: CloudMapGraphVisitor,
    ) -> None:
        self.db = db
        self.visitor = visitor

    def walk(self, start_url: str = "") -> None:
        """Walk the graph, optionally starting from a specific URL."""
        if start_url:
            all_found = self._find_all_records(start_url)
            if not all_found:
                self.visitor.not_found(start_url)
                return
            visited: set = {start_url}
            for kind, record in all_found:
                self.visitor.visit_record(kind, start_url, record)
                self._walk_edges(record, kind, visited)
                self.visitor.leave_record(kind, start_url, record)
        else:
            self.visitor.start_graph("CloudMap")
            visited = set()
            for section_name, collection in (
                ("Repositories", self.db.repositories),
                ("Artifacts", self.db.artifacts),
                ("Instantiations", self.db.instantiations),
                ("Services", self.db.services),
                ("Types", self.db.types),
            ):
                if not collection:
                    continue
                self.visitor.start_section(section_name)
                for url, record in collection.items():
                    cls_name = record.__class__.__name__
                    kind = "Type" if cls_name == "CloudType" else cls_name
                    if url in visited:
                        self.visitor.visit_record(kind, url, record, seen=True)
                        continue
                    visited.add(url)
                    self.visitor.visit_record(kind, url, record)
                    self._walk_edges(record, kind, visited)
                    self.visitor.leave_record(kind, url, record)
            self.visitor.end_graph(empty=not visited)

    def _find_record(self, url: str) -> Optional[Tuple[str, Any]]:
        for kind, collection in (
            ("Service", self.db.services),
            ("Instantiation", self.db.instantiations),
            ("Artifact", self.db.artifacts),
            ("Repository", self.db.repositories),
            ("Type", self.db.types),
        ):
            if url in collection:
                return kind, collection[url]
        if url.startswith("git:") and not url.endswith(".git") and "#" not in url:
            git_url = url + ".git"
            if git_url in self.db.repositories:
                return "Repository", self.db.repositories[git_url]
        return None

    def _find_all_records(self, url: str) -> List[Tuple[str, Any]]:
        results: List[Tuple[str, Any]] = []
        for kind, collection in (
            ("Instantiation", self.db.instantiations),
            ("Repository", self.db.repositories),
            ("Artifact", self.db.artifacts),
            ("Service", self.db.services),
            ("Type", self.db.types),
        ):
            if url in collection:
                results.append((kind, collection[url]))
        return results

    @staticmethod
    def _is_url(key: str) -> bool:
        return "://" in key or key.startswith("pkg:")

    def _walk_typed_urls(
        self, label: str, typed_urls: Dict[str, Any], visited: set
    ) -> None:
        if not typed_urls:
            return
        self.visitor.visit_relationship(label)
        for key, tr in typed_urls.items():
            tr_pairs: Optional[List[Tuple[str, Optional[Dict[str, Any]]]]] = None
            if isinstance(tr, TypeRefs) and tr.types:
                tr_pairs = [(n, dict(c) if c else None) for n, c in tr.types.items()]
            if self._is_url(key):
                # Key is a URL — walk as a record ref with type_refs metadata
                self._walk_child(key, visited, type_refs=tr_pairs)
            elif tr_pairs:
                # Key is a label — each type name becomes a type ref
                for name, constraint in tr_pairs:
                    self.visitor.visit_type_ref(name, constraint, label=key)
                    self._walk_child(name, visited, _walk_only=True)
                    self.visitor.leave_type_ref()
            else:
                self.visitor.visit_label(key)
        self.visitor.leave_relationship(label)

    def _walk_edges(self, record: Any, kind: str, visited: set) -> None:
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
                self._walk_typed_urls("notable", record.notable, visited)
            if record.references:
                self._walk_typed_urls("references", record.references, visited)
            if record.dependencies:
                self._walk_typed_urls("dependencies", record.dependencies, visited)
            if record.instantiates and record.instantiates.types:
                self.visitor.visit_relationship("instantiates")
                for name, c in record.instantiates.types.items():
                    self.visitor.visit_type_ref(name, dict(c) if c else None)
                    self._walk_child(name, visited, _walk_only=True)
                    self.visitor.leave_type_ref()
                self.visitor.leave_relationship("instantiates")
            if record.instantiated_by:
                self._walk_typed_urls(
                    "instantiated_by", record.instantiated_by, visited
                )

        elif kind == "Instantiation":
            assert isinstance(record, Instantiation)
            if record.source:
                self.visitor.visit_relationship("source")
                self._walk_child(record.source, visited)
                self.visitor.leave_relationship("source")
            if record.instantiated:
                self._walk_typed_urls("instantiated", record.instantiated, visited)
            if record.inputs:
                self._walk_typed_urls("inputs", record.inputs, visited)

        elif kind == "Service":
            assert isinstance(record, Service)
            if record.connections:
                self._walk_typed_urls("connections", record.connections, visited)
            if record.instantiated_by:
                self._walk_typed_urls(
                    "instantiated_by", record.instantiated_by, visited
                )

        elif kind == "Type":
            assert isinstance(record, CloudType)
            if record.extends:
                filtered = [n for n in record.extends if n != record.name]
                if filtered:
                    self.visitor.visit_relationship("extends")
                    for name in filtered[:1]:
                        self.visitor.visit_label(name)
                    self.visitor.leave_relationship("extends")
            if record.source:
                edges.append(("source", [record.source]))
            if record.model:
                edges.append(("model", [record.model]))
            if record.implementations:
                edges.append(("implementations", list(record.implementations)))

        for label, urls in edges:
            if not urls:
                continue
            self.visitor.visit_relationship(label)
            for url in urls:
                self._walk_child(url, visited)
            self.visitor.leave_relationship(label)

    def _walk_child(
        self,
        url: str,
        visited: set,
        type_refs: Optional[List[Tuple[str, Optional[Dict[str, Any]]]]] = None,
        _walk_only: bool = False,
    ) -> None:
        """Walk a child record.

        When *_walk_only* is True the record's edges are walked (to populate
        sections) but no ref is added to the current relationship list — a
        ``visit_type_ref`` call should have already been made.
        """
        found = self._find_record(url)
        if found is None:
            if not _walk_only:
                self.visitor.visit_ref(url, missing=True, type_refs=type_refs)
            return
        child_kind, child_record = found
        if url in visited:
            if not _walk_only:
                self.visitor.visit_record(
                    child_kind, url, child_record, seen=True, type_refs=type_refs
                )
            return
        visited.add(url)
        self.visitor.visit_record(
            child_kind, url, child_record, type_refs=type_refs, walk_only=_walk_only
        )
        self._walk_edges(child_record, child_kind, visited)
        self.visitor.leave_record(child_kind, url, child_record)


def walk_cloudmap_graph(
    db: "CloudMapDB",
    visitor: CloudMapGraphVisitor,
    start_url: str = "",
) -> None:
    """Walk the CloudMap graph calling visitor methods for each record and edge."""
    CloudMapGraphWalker(db, visitor).walk(start_url)


class RichTreeVisitor(CloudMapGraphVisitor):
    """Visitor that builds a rich.Tree for console output."""

    _KIND_STYLES: Dict[str, str] = {
        "Repository": "green",
        "Artifact": "cyan",
        "Instantiation": "yellow",
        "Service": "magenta",
        "Type": "blue",
    }

    def __init__(self, console: Optional[Console] = None) -> None:
        self.console = console or getConsole()
        self._stack: List[Tree] = []
        self._root: Optional[Tree] = None
        self._type_ref_depth: int = 0

    def _current(self) -> Tree:
        return self._stack[-1]

    def _label(
        self, kind: str, url: str, record: "CloudMapRecord", guide_style: str = ""
    ) -> str:

        title = ""
        if hasattr(record, "metadata") and hasattr(record.metadata, "title"):
            title = record.metadata.title or ""
        elif hasattr(record, "name"):
            title = record.name
        prefix = f"{title} " if title and title != url else ""
        type_str = ""
        if hasattr(record, "type") and isinstance(record.type, TypeRefs):
            names = [
                f"{name}{(' v' + str(ref['version'])).lstrip('v') if ref and 'version' in ref else ''}"
                for name, ref in record.type.types.items()
            ]
            if names or prefix:
                pad = " " * len(kind)
                styled_pipe = f"[{guide_style}]\u2502[/]" if guide_style else "\u2502"
                type_str = (
                    f"\n{styled_pipe}{pad}{escape(prefix)}({escape(', '.join(names))})"
                )
        return f"[bold]{kind}[/] {escape(url)}{type_str}"

    def start_graph(self, title: str) -> None:
        self._root = Tree(f"[bold]{title}[/]")
        self._stack = [self._root]

    def end_graph(self, empty: bool) -> None:
        if empty:
            self.console.print("[dim]No records found in CloudMap.[/]")
        elif self._root:
            self.console.print(self._root)

    def start_section(self, name: str) -> None:
        section = self._stack[0].add(f"[bold]{name}[/]")
        # Replace stack to: [root, section]
        self._stack = [self._stack[0], section]

    def visit_record(
        self,
        kind: str,
        url: str,
        record: "CloudMapRecord",
        *,
        seen: bool = False,
        type_refs: Optional[List[Tuple[str, Optional[Dict[str, Any]]]]] = None,
        walk_only: bool = False,
    ) -> None:
        style = self._KIND_STYLES.get(kind, "")
        label = self._label(kind, url, record, style)
        if type_refs:
            names = ", ".join(n for n, _ in type_refs)
            label += f" [dim]({escape(names)})[/]"
        if seen:
            parent = self._stack[-1] if self._stack else None
            if parent:
                parent.add(f"{label} [dim]\\[seen][/]")
            return
        if walk_only:
            # Record is being walked only for its edges — visit_type_ref
            # already pushed a node, so just reuse it.
            return
        if self._stack:
            node = self._stack[-1].add(label, guide_style=style)
        else:
            node = Tree(label, guide_style=style)
            self._root = node
            # For start_url mode, print each root immediately
            self._deferred_print = node
        self._stack.append(node)

    def leave_record(self, kind: str, url: str, record: "CloudMapRecord") -> None:
        left = self._stack.pop()
        if not self._stack and hasattr(self, "_deferred_print"):
            self.console.print(self._deferred_print)
            del self._deferred_print

    def visit_relationship(self, label: str) -> None:
        branch = self._current().add(f"[dim]{label}[/]")
        self._stack.append(branch)

    def leave_relationship(self, label: str) -> None:
        self._stack.pop()

    def visit_label(
        self,
        label: str,
    ) -> None:
        self._current().add(f"[dim italic]{escape(label)}[/]")

    def visit_ref(
        self,
        url: str,
        *,
        missing: bool = False,
        type_refs: Optional[List[Tuple[str, Optional[Dict[str, Any]]]]] = None,
    ) -> None:
        suffix = ""
        if type_refs:
            names = ", ".join(n for n, _ in type_refs)
            suffix = f" ({escape(names)})"
        if missing:
            suffix += " \\[missing]"
        self._current().add(f"[dim italic]{escape(url)}{suffix}[/]")

    def visit_type_ref(
        self,
        name: str,
        constraints: Optional[Dict[str, Any]] = None,
        *,
        label: str = "",
    ) -> None:
        prefix = f"{label}: " if label else ""
        version = ""
        if constraints and "version" in constraints:
            version = f" v{constraints['version']}"
        text = f"[dim italic]{escape(prefix)}{escape(name)}{escape(version)}[/]"
        node = self._current().add(text)
        # Push so that a subsequent walk_only record nests its edges here;
        # _type_ref_depth tracks the stack depth so leave_type_ref knows
        # whether leave_record already popped it.
        self._stack.append(node)
        self._type_ref_depth = len(self._stack)

    def leave_type_ref(self) -> None:
        # Only pop if leave_record hasn't already done so
        if len(self._stack) >= self._type_ref_depth:
            self._stack.pop()
        self._type_ref_depth = 0

    def not_found(self, url: str) -> None:
        self.console.print(f"[red]Record not found:[/] {escape(url)}")


from .server.schemas import (
    GraphJson,
    GraphNodeJson,
    RecordRef,
    RelEntry,
    TypeRefJson,
)

_SECTION_FOR_KIND: Dict[str, str] = {
    "Repository": "Repositories",
    "Artifact": "Artifacts",
    "Instantiation": "Instantiations",
    "Service": "Services",
    "Type": "Types",
}


class JsonGraphVisitor(CloudMapGraphVisitor):
    """Visitor that builds a JSON-serializable graph representation.

    Records are stored in ``sections`` (keyed by kind → url → node).
    Relationship lists contain lightweight ``RecordRef`` or ``TypeRefJson`` entries.
    """

    def __init__(self) -> None:
        # _stack holds (node, rel_label) pairs; rel_label is the key on node's
        # rels dict where entries should be appended, or "" for the root level.
        self._stack: List[Tuple[GraphNodeJson, str]] = []
        self._current_section_name: str = ""
        self.result: GraphJson = {}

    def _sections(self) -> Dict[str, Dict[str, GraphNodeJson]]:
        return self.result.setdefault("sections", {})

    def _ensure_record(self, kind: str, url: str) -> GraphNodeJson:
        """Return the node for *url*, creating it in the appropriate section if new."""
        section_name = _SECTION_FOR_KIND.get(kind, kind)
        section = self._sections().setdefault(section_name, {})
        if url not in section:
            section[url] = GraphNodeJson(kind=kind, url=url)
        return section[url]

    def _current_rel_list(self) -> Optional[List[RelEntry]]:
        """Get the rels list where new entries should be appended, or None at section top-level."""
        if not self._stack:
            if "roots" in self.result:
                return cast(List[RelEntry], self.result["roots"])
            return None
        node, rel_label = self._stack[-1]
        if rel_label:
            rels = node.setdefault("rels", {})
            return rels.setdefault(rel_label, [])
        return None

    def start_graph(self, title: str) -> None:
        self.result = {}

    def end_graph(self, empty: bool) -> None:
        pass

    def start_section(self, name: str) -> None:
        self._current_section_name = name
        self._sections().setdefault(name, {})

    def visit_record(
        self,
        kind: str,
        url: str,
        record: "CloudMapRecord",
        *,
        seen: bool = False,
        type_refs: Optional[List[Tuple[str, Optional[Dict[str, Any]]]]] = None,
        walk_only: bool = False,
    ) -> None:
        node = self._ensure_record(kind, url)
        if not walk_only:
            # Add a ref to the current rels list (roots or parent rel); skip at section top-level
            rel_list = self._current_rel_list()
            if rel_list is not None:
                ref = RecordRef(url=url, kind=kind)
                if type_refs:
                    ref["type_refs"] = [_make_type_ref_json(n, c) for n, c in type_refs]
                rel_list.append(ref)

        if not seen:
            self._stack.append((node, ""))

    def leave_record(self, kind: str, url: str, record: "CloudMapRecord") -> None:
        self._stack.pop()

    def visit_relationship(self, label: str) -> None:
        # Push the current record node again with this rel label,
        # so entries are added under node.rels[label].
        node = self._stack[-1][0]
        self._stack.append((node, label))

    def leave_relationship(self, label: str) -> None:
        self._stack.pop()

    def visit_label(
        self,
        label: str,
    ) -> None:
        rel_list = self._current_rel_list()
        if rel_list is not None:
            rel_list.append(label)

    def visit_ref(
        self,
        url: str,
        *,
        missing: bool = False,
        type_refs: Optional[List[Tuple[str, Optional[Dict[str, Any]]]]] = None,
    ) -> None:
        rel_list = self._current_rel_list()
        if rel_list is not None:
            ref = RecordRef(url=url)
            if missing:
                ref["missing"] = missing
            if type_refs:
                ref["type_refs"] = [_make_type_ref_json(n, c) for n, c in type_refs]
            rel_list.append(ref)

    def visit_type_ref(
        self,
        name: str,
        constraints: Optional[Dict[str, Any]] = None,
        *,
        label: str = "",
    ) -> None:
        rel_list = self._current_rel_list()
        if rel_list is not None:
            rel_list.append(_make_type_ref_json(name, constraints, label))

    def not_found(self, url: str) -> None:
        self.result = GraphJson(error=f"Record not found: {url}")


def _make_type_ref_json(
    name: str,
    constraints: Optional[Dict[str, Any]] = None,
    label: str = "",
) -> TypeRefJson:
    ref = TypeRefJson(type=name)
    if constraints:
        ref["constraints"] = constraints
    if label:
        ref["label"] = label
    return ref


def cloudmap_graph_json(db: "CloudMapDB", start_url: str = "") -> GraphJson:
    """Return a JSON-serializable graph of the CloudMap."""
    visitor = JsonGraphVisitor()
    if start_url:
        visitor.result["roots"] = []
    walk_cloudmap_graph(db, visitor, start_url)
    return visitor.result


def cloudmap_graph_console(
    db: "CloudMapDB", start_url: str = "", console: Optional["Console"] = None
) -> None:
    """Print a rich.Tree showing how cloudmap records reference each other."""
    visitor = RichTreeVisitor(console)
    walk_cloudmap_graph(db, visitor, start_url)
