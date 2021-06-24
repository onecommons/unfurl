# Copyright (c) 2021 Adam Souzis
# SPDX-License-Identifier: MIT
import collections
from .util import UnfurlError, UnfurlTaskError


class PlanRequest(object):
    error = None
    future_dependencies = ()

    def __init__(self, target):
        self.target = target

    def update_future_dependencies(self, completed):
        return self.future_dependencies


class TaskRequest(PlanRequest):
    """
    Yield this to run a child task. (see :py:meth:`unfurl.configurator.TaskView.create_sub_task`)
    """

    def __init__(
        self,
        configSpec,
        target,
        reason,
        persist=False,
        required=None,
        startState=None,
    ):
        super(TaskRequest, self).__init__(target)
        self.configSpec = configSpec
        self.reason = reason
        self.persist = persist
        self.required = required
        self.error = configSpec.name == "#error"
        self.startState = startState
        self.task = None

    @property
    def name(self):
        if self.configSpec.operation:
            name = self.configSpec.operation
        else:
            name = self.configSpec.name
        if self.reason and self.reason not in name:
            return name + " (reason: " + self.reason + ")"
        return name

    def update_future_dependencies(self, completed):
        self.future_dependencies = [
            fr for fr in self.future_dependencies if fr not in completed
        ]
        return self.future_dependencies

    def _summary_dict(self):
        return dict(
            operation=self.configSpec.operation or self.configSpec.name,
            reason=self.reason,
        )

    def __repr__(self):
        return "TaskRequest(%s(%s,%s):%s)" % (
            self.target,
            self.target.status,
            self.target.state,
            self.name,
        )


class SetStateRequest(PlanRequest):
    def __init__(self, target, state):
        super(SetStateRequest, self).__init__(target)
        self.set_state = state

    @property
    def name(self):
        return self.set_state

    def _summary_dict(self):
        return dict(set_state=self.set_state)


class TaskRequestGroup(PlanRequest):
    def __init__(self, target, workflow):
        super(TaskRequestGroup, self).__init__(target)
        self.workflow = workflow
        self.children = []

    @property
    def future_dependencies(self):
        future_dependencies = []
        for req in self.children:
            future_dependencies.extend(req.future_dependencies)
        return future_dependencies

    def update_future_dependencies(self, completed):
        future_dependencies = []
        for req in self.children:
            future_dependencies.extend(req.update_future_dependencies(completed))
        return future_dependencies

    def __repr__(self):
        return "TaskRequestGroup(%s:%s:%s)" % (
            self.target,
            self.workflow,
            self.children,
        )


class JobRequest(object):
    """
    Yield this to run a child job.
    """

    def __init__(self, resources, errors):
        self.instances = resources
        self.errors = errors

    def __repr__(self):
        return "JobRequest(%s)" % (self.instances,)


def get_render_requests(requests):
    # returns requests that can be rendered grouped by its top-most task group
    for req in requests:
        if isinstance(req, TaskRequestGroup):
            for parent, child in get_render_requests(req.children):
                yield req, child  # yields root as parent
        elif isinstance(req, TaskRequest):
            yield None, req


def _get_deps(parent, req, liveDependencies, future_requests):
    previous = None
    for (root, r) in future_requests:
        if r.target.key in liveDependencies:
            if root:
                if previous is root or parent is root:
                    # only yield root once and
                    # don't consider requests in the same root
                    continue
                previous = root
            yield root or r


def set_fulfilled(requests, completed):
    # requests, completed are top level requests,
    # as is future_dependencies
    ready, notReady = [], []
    for req in requests:
        if req.update_future_dependencies(completed):
            notReady.append(req)
        else:  # list is now empty so request is ready
            ready.append(req)
    return ready, notReady


def _render_request(job, parent, req, future_requests):
    # req is a taskrequests, future_requests are (grouprequest, taskrequest) pairs
    if req.task:
        task = req.task
        task.target.root.attributeManager = task._attributeManager
    else:
        task = req.task = job.create_task(req.configSpec, req.target, reason=req.reason)
    task.logger.debug("rendering %s %s", task.target.name, task.name)
    try:
        task.rendered = task.configurator.render(task)
    except Exception:
        if task._workFolder:
            task._workFolder.failed()
        task._inputs = None
        task._attributeManager.attributes = {}  # rollback changes
        return [], UnfurlTaskError(task, "configurator.render failed")
    else:
        if parent and parent.workflow == "undeploy":
            # when removing an instance don't worry about depending values changing in the future
            deps = None
        else:
            # key => (instance, list<attribute>)
            liveDependencies = task._attributeManager.find_live_dependencies()
            # a future request may change the value of these attributes
            deps = list(_get_deps(parent, req, liveDependencies, future_requests))

        if deps:
            req.future_dependencies = deps
            task.logger.debug(
                "%s can not render yet, depends on %s", task.target.name, str(deps)
            )
            # rollback changes:
            task._inputs = None
            task._attributeManager.attributes = {}
            if task._workFolder:
                task._workFolder.discard()
            return deps, None
        else:
            task.commit_changes()
            if task._workFolder:
                task._workFolder.apply()
    return [], None


def _add_to_req_list(reqs, parent, request):
    if parent:  # only add if we haven't already
        if not reqs or reqs[-1] is not parent:
            reqs.append(parent)
    else:
        reqs.append(request)


def do_render_requests(job, requests):
    ready, notReady, errors = [], [], []
    render_requests = collections.deque(get_render_requests(requests))
    while render_requests:
        parent, request = render_requests.popleft()
        deps, error = _render_request(job, parent, request, render_requests)
        if error:
            errors.append(error)
        elif deps:
            # remove if we already added the parent
            if parent and ready and ready[-1] is parent:
                ready.pop()
            _add_to_req_list(notReady, parent, request)
        else:
            if not parent or not notReady or notReady[-1] is not parent:
                # don't add if the parent was placed on the notReady list
                _add_to_req_list(ready, parent, request)
    return ready, notReady, errors
