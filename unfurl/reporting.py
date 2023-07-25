# Copyright (c) 2022 Adam Souzis
# SPDX-License-Identifier: MIT
import itertools
import json
from typing import Any, Dict, Iterable, List, Tuple, Union, Optional, TYPE_CHECKING
from .runtime import EntityInstance, NodeInstance
from .planrequests import (
    TaskRequest,
    TaskRequestGroup,
    JobRequest,
)
from .support import Status
from .logs import getLogger, getConsole
from rich.console import Console
from rich.table import Table
from rich import box
from rich.segment import Segment
from rich.markup import escape
import re
import os

if TYPE_CHECKING:
    from .yamlmanifest import YamlManifest
    from rich.console import RenderableType
    from rich.style import StyleType
    from .job import Job

logger = getLogger("unfurl")


class JobTable(Table):
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
                    # XXX how to center extra? how to turn on automatic formatting?
                    #     how to wrap lines and yield more than one line?
                    text = console.render_str(extra)
                    for segment_list in console.render_lines(text, options):
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
                node["job_request"] = manifest.path
            else:
                node["job_request"] = "local"
            if request.target:
                node["status"] = str(request.target.status)
            yield node

    @staticmethod
    def _switch_target(
        target: NodeInstance, old_summary_list: List[dict]
    ) -> List[dict]:
        new_summary_list: List[dict] = []
        node = dict(
            instance=target.name,
            status=str(target.status),
            state=str(target.state),  # type: ignore
            managed=target.created,
            plan=new_summary_list,
        )
        old_summary_list.append(node)
        return new_summary_list

    @staticmethod
    def _list_plan_summary(
        requests: List[JobRequest],
        target: NodeInstance,
        parent_summary_list: List[dict],
        include_rendered: bool,
        workflow: str,
    ) -> None:
        summary_list = parent_summary_list
        for request in requests:
            if isinstance(request, JobRequest):
                summary_list.extend(JobReporter._job_request_summary([request], None))
                continue
            isGroup = isinstance(request, TaskRequestGroup)
            if isGroup and not request.children:
                continue  # don't include in the plan
            if request.target is not target:
                if workflow == "deploy" and not request.include_in_plan():
                    continue
                # target changed, add it to the parent's list
                # switch to the "plan" member of the new target
                target = request.target
                summary_list = JobReporter._switch_target(target, parent_summary_list)
            if isGroup:
                sequence = []
                group = {}
                if request.workflow:
                    group["workflow"] = str(request.workflow)
                group["sequence"] = sequence
                summary_list.append(group)
                JobReporter._list_plan_summary(
                    request.children, target, sequence, include_rendered, workflow
                )
            else:
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
        for (m, requests) in job.external_requests:  # type: ignore
            summary.extend(JobReporter._job_request_summary(requests, m))
        JobReporter._list_plan_summary(job.plan_requests, None, summary, include_rendered, job.workflow)  # type: ignore
        if not pretty:
            return summary
        else:
            return json.dumps(summary, indent=2)

    @staticmethod
    def stats(tasks, asMessage: bool = False) -> Union[Dict[str, int], str]:
        # note: the status of the task, not the target resource
        key = (
            lambda t: Status.error
            if t.target_status == Status.error
            else t._localStatus or Status.unknown
        )
        tasks = sorted(tasks, key=key)  # type: ignore
        stats = dict(total=len(tasks), ok=0, error=0, unknown=0, skipped=0)
        for k, g in itertools.groupby(tasks, key):
            if not k:  # is a Status
                stats["skipped"] = len(list(g))
            else:
                stats[k.name] = len(list(g))
        stats["changed"] = len([t for t in tasks if t.modified_target])
        if asMessage:
            return "{total} tasks ({changed} changed, {ok} ok, {error} failed, {unknown} unknown, {skipped} skipped)".format(
                **stats
            )
        return stats

    @staticmethod
    def plan_summary(
        job: "Job",
        plan_requests: List[TaskRequest],
        external_requests: Iterable[Tuple[Any, Any]],
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
            requests: List[Union[JobRequest, TaskRequest, TaskRequestGroup]],
            target: Optional[EntityInstance],
            indent: int,
        ) -> None:
            nonlocal count
            for request in requests:
                isGroup = isinstance(request, TaskRequestGroup)
                if isGroup and not request.children:  # type: ignore
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
                                target.status.name if target.status is not None else "",  # type: ignore
                                target.state.name if target.state is not None else "",  # type: ignore
                                "managed" if target.created else "",  # type: ignore
                            ),
                        )
                    )
                    nodeStr = f'Node "{target.template.nested_name}" ({status}):'  # type: ignore
                    output.append(" " * indent + nodeStr)
                if isGroup:
                    output.append(
                        "%s- %s:" % (" " * indent, (request.workflow or "sequence"))  # type: ignore
                    )
                    _summary(request.children, target, indent + INDENT)  # type: ignore
                else:
                    count += 1
                    output.append(" " * indent + f"- operation {request.name}")  # type: ignore
                    if request.task:
                        if request.task._workFolders:
                            for wf in request.task._workFolders.values():
                                output.append(" " * indent + f"   rendered at {wf.cwd}")
                        if request.not_ready:
                            output.append(
                                " " * indent + "   (render waiting for dependents)"
                            )
                        elif request.task._errors:  # don't report error if waiting
                            output.append(" " * indent + "   (errors while rendering)")

        opts = job.jobOptions.get_user_settings()
        options = ",".join([f"{k} = {opts[k]}" for k in opts if k != "planOnly"])
        header = f"Plan for {job.workflow}"  # type: ignore
        if options:
            header += f" ({options})"
        output: List[str] = [header + ":\n"]

        for m, jr in external_requests:
            if jr:
                count += 1
                output += [f" External jobs on {m.path}:"]
                for j in jr:
                    output.append(" " * INDENT + j.name)

        _summary(plan_requests, None, 0)  # type: ignore
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
            job.timeElapsed,
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
                task_success = (
                    "[green]success[/]" if task.result.success else "[red]failed[/]"
                )
            else:
                task_success = "[white]skipped[/]"
            operation = task.configSpec.operation
            reason = task.reason or ""
            resource = task.target.name
            if task.status is None:
                status = ""
            else:
                status = f"[{task.status.color}]{task.status.name.upper()}[/]"
            state = task.target_state and task.target_state.name or ""
            changed = "[green]Yes[/]" if task.modified_target else "[white]No[/]"
            if task.result and task.result.result:
                result = escape(f"Output: {task.result.result}")
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
