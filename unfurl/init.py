import uuid
import os
import os.path
import sys
import shutil
from . import __version__, DefaultNames, getHomeConfigPath
from .tosca import TOSCA_VERSION
from .repo import Repo, GitRepo
from .util import UnfurlError
from .localenv import LocalEnv, Project

_templatePath = os.path.join(os.path.abspath(os.path.dirname(__file__)), "templates")


def _writeFile(folder, filename, content):
    if not os.path.isdir(folder):
        os.makedirs(folder)
    filepath = os.path.join(folder, filename)
    with open(filepath, "w") as f:
        f.write(content)
    return filepath


def writeTemplate(folder, filename, templatePath, vars):
    from .runtime import NodeInstance
    from .eval import RefContext
    from .support import applyTemplate

    with open(os.path.join(_templatePath, templatePath)) as f:
        source = f.read()
    instance = NodeInstance()
    instance._baseDir = _templatePath
    content = applyTemplate(source, RefContext(instance, vars))
    return _writeFile(folder, filename, content)


def writeProjectConfig(
    projectdir,
    filename=DefaultNames.LocalConfig,
    defaultManifestPath=DefaultNames.Ensemble,
    localInclude="",
    templatePath=DefaultNames.LocalConfig + ".j2",
):
    vars = dict(
        version=__version__, include=localInclude, manifestPath=defaultManifestPath
    )
    return writeTemplate(projectdir, filename, templatePath, vars)


def createHome(path=None, **kw):
    """
    Write ~/.unfurl_home/unfurl.yaml if missing
    """
    homePath = getHomeConfigPath(path)
    if not homePath or os.path.exists(homePath):
        return None
    homedir, filename = os.path.split(homePath)
    writeTemplate(homedir, DefaultNames.Ensemble, "home-manifest.yaml.j2", {})
    configPath = writeProjectConfig(
        homedir, filename, templatePath="home-unfurl.yaml.j2"
    )
    if not kw.get("no_engine"):
        initEngine(homedir, kw.get("engine") or "venv:")
    return configPath


def _createRepo(repotype, gitDir, gitUri=None):
    from git import Repo

    if not os.path.isdir(gitDir):
        os.makedirs(gitDir)
    repo = Repo.init(gitDir)
    filename = ".unfurl"
    filepath = os.path.join(gitDir, filename)
    with open(filepath, "w") as f:
        f.write(
            """\
  unfurl:
    version: %s
  repo:
    type: %s
    uuid: %s
  """
            % (__version__, repotype, uuid.uuid1())
        )

    repo.index.add([filename])
    repo.index.commit("Initial Commit")
    return GitRepo(repo)


def writeServiceTemplate(projectdir, repo):
    # path to spec folder relative to spec repository's root
    relPathToSpecRepo = os.path.relpath(os.path.abspath(projectdir), repo.workingDir)
    vars = dict(
        version=TOSCA_VERSION,
        specRepoPath=relPathToSpecRepo,
        initialCommit=repo.getInitialRevision(),
    )
    return writeTemplate(
        projectdir, "service-template.yaml", "service-template.yaml.j2", vars
    )


def createSpecRepo(gitDir):
    repo = _createRepo(DefaultNames.SpecDirectory, gitDir)
    writeServiceTemplate(gitDir, repo)
    manifestTemplate = DefaultNames.EnsembleTemplate
    writeTemplate(gitDir, manifestTemplate, "manifest-template.yaml.j2", {})
    repo.repo.index.add(["service-template.yaml", manifestTemplate])
    repo.repo.index.commit("Default specification repository boilerplate")
    return repo


def writeMultiSpecManifest(
    destDir, manifestName, specRepo, instanceRepo, specDir=None, extraVars=None
):
    # path to spec folder relative to spec repository's root
    relPathToSpecRepo = specDir and os.path.relpath(specDir, specRepo.workingDir)

    # path to ensemble folder relative to ensemble repository's root
    relPathToInstanceRepo = instanceRepo and os.path.relpath(
        destDir, instanceRepo.workingDir
    )
    if extraVars is None:
        # default behaviour is to include the ensembleTemplate
        extraVars = dict(ensembleTemplate=DefaultNames.EnsembleTemplate)
    vars = dict(
        specRepoPath=relPathToSpecRepo or "",
        instanceRepoPath=relPathToInstanceRepo or "",
        specInitialCommit=specRepo.getInitialRevision(),
        instanceInitialCommit=instanceRepo and instanceRepo.getInitialRevision(),
    )
    vars.update(extraVars)
    return writeTemplate(destDir, manifestName, "manifest.yaml.j2", vars)


def createInstanceRepo(gitDir, specRepo):
    manifestName = DefaultNames.Ensemble
    repo = _createRepo("ensemble", gitDir)
    writeMultiSpecManifest(gitDir, manifestName, specRepo, repo)
    return initInstanceRepo(repo, os.path.join(gitDir, manifestName))


def initInstanceRepo(repo, manifestPath):
    dir, manifestName = os.path.split(manifestPath)
    gitAttributesContent = "**/*%s merge=union\n" % DefaultNames.JobsLog
    _writeFile(dir, ".gitattributes", gitAttributesContent)
    repo.repo.index.add([manifestName, ".gitattributes"])
    repo.repo.index.commit("Default ensemble repository boilerplate")
    return repo


def createMultiRepoProject(projectdir, specRepo=None, addDefaults=True):
    """
  Creates a project folder with two git repositories:
  a specification repository that contains "service-template.yaml" and "ensemble-template.yaml" in a "spec" folder.
  and an ensemble repository containing a "ensemble.yaml" in a folder named "ensemble1".
  """
    defaultManifestPath = None
    if addDefaults:
        defaultManifestPath = os.path.join(
            DefaultNames.EnsembleDirectory, DefaultNames.Ensemble
        )
        if not specRepo:
            specRepo = createSpecRepo(
                os.path.join(projectdir, DefaultNames.SpecDirectory)
            )
        createInstanceRepo(
            os.path.join(projectdir, DefaultNames.EnsembleDirectory), specRepo
        )
    projectConfigPath = writeProjectConfig(
        projectdir, defaultManifestPath=defaultManifestPath
    )
    return projectConfigPath


def createMonoRepoProject(projectdir, repo, addDefaults=True):
    """
    Creates a folder named `projectdir` with a git repository with the following files:

    unfurl.yaml
    unfurl.local.example.yaml
    .gitignore
    ensemble.yaml

    Returns the absolute path to unfurl.yaml
    """
    localConfigFilename = "unfurl.local.yaml"
    writeProjectConfig(
        projectdir, localConfigFilename, templatePath="unfurl.local.yaml.j2"
    )
    defaultManifestPath = DefaultNames.Ensemble if addDefaults else None
    localInclude = "+?include: " + localConfigFilename
    projectConfigPath = writeProjectConfig(
        projectdir, localInclude=localInclude, defaultManifestPath=defaultManifestPath
    )
    gitIgnoreContent = """%s\nlocal\nrepos\n""" % localConfigFilename
    gitIgnorePath = _writeFile(projectdir, ".gitignore", gitIgnoreContent)
    gitAttributesContent = "**/*%s merge=union\n" % DefaultNames.JobsLog
    gitAttributesPath = _writeFile(projectdir, ".gitattributes", gitAttributesContent)
    files = [projectConfigPath, gitIgnorePath, gitAttributesPath]
    if addDefaults:
        serviceTemplatePath = writeServiceTemplate(projectdir, repo)
        # write manifest
        manifestPath = writeTemplate(
            projectdir, DefaultNames.Ensemble, "manifest-template.yaml.j2", {}
        )
        files.extend([serviceTemplatePath, manifestPath])

    repo.commitFiles(files, "Create a new unfurl repository")
    return projectConfigPath


def createProject(
    projectdir, home=None, mono=False, existing=False, addDefaults=True, **kw
):
    if existing:
        repo = Repo.findContainingRepo(projectdir)
        if not repo:
            raise UnfurlError("Could not find an existing repository")
    else:
        repo = None
    # creates home if it doesn't exist already:
    newHome = createHome(home, **kw)
    # XXX add project to ~/.unfurl_home/unfurl.yaml
    if mono or existing:
        if not repo:
            repo = _createRepo("mono", projectdir)
        projectConfigPath = createMonoRepoProject(
            projectdir, repo, addDefaults=addDefaults
        )
    else:
        projectConfigPath = createMultiRepoProject(projectdir, addDefaults=addDefaults)

    if not newHome and not kw.get("no_engine") and kw.get("engine"):
        # if engine was explicitly set and we aren't creating the home project
        # then initialize the engine here
        initEngine(projectdir, kw.get("engine"))

    return newHome, projectConfigPath


def _looksLike(path, name):
    # in case path is a directory:
    if os.path.isfile(os.path.join(path, name)):
        return path, name
    if path.endswith(name):
        return os.path.split(path)
    return None


def initNewEnsemble(manifest, sourceProject, targetProject):
    # We need to clone repositories that are local to the source project
    for repoSpec in manifest.repositories.values():
        if repoSpec.name == "self":
            continue
        repo = sourceProject.findRepository(repoSpec)
        if repo:
            targetProject.findOrClone(repo)

    destDir = os.path.dirname(manifest.manifest.path)
    if targetProject.projectRepo:
        repo = targetProject.projectRepo
        relPath, revision, bare = repo.findPath(destDir)
    else:
        repo = _createRepo("ensemble", destDir)
        relPath = ""
    manifest.addRepo("self", dict(url=repo.getGitLocalUrl(relPath, "ensemble")))

    with open(manifest.manifest.path, "w") as f:
        manifest.dump(f)

    initInstanceRepo(repo, manifest.manifest.path)
    return repo


def createNewEnsemble(sourcePath, sourceProject, targetPath, targetProject):
    """
    If path is relative in
    include directives, imports, or external file reference they are guaranteed to be local to the project.
    Outside paths need to be referenced with a named repository.
    """
    from unfurl import yamlmanifest

    if not targetPath:
        destDir, manifestName = DefaultNames.EnsembleDirectory, DefaultNames.Ensemble
        targetPath = os.path.join(destDir, manifestName)
    elif targetPath.endswith(".yaml") or targetPath.endswith(".yml"):
        destDir, manifestName = os.path.split(targetPath)
    else:
        destDir = targetPath
        manifestName = DefaultNames.Ensemble
        targetPath = os.path.join(destDir, manifestName)

    templateVars = None
    # look for an ensemble-template or service-template in source path
    template = _looksLike(sourcePath, DefaultNames.EnsembleTemplate)
    if template:
        templateVars = dict(ensembleTemplate=template[1])
    template = _looksLike(sourcePath, DefaultNames.ServiceTemplate)
    if template:
        templateVars = dict(serviceTemplate=template[1])

    if templateVars:
        # we found a template file to clone
        assert sourceProject
        sourceDir = template[0]
        specRepo, relPath, revision, bare = sourceProject.findPathInRepos(sourceDir)
        if not specRepo:
            raise UnfurlError("Cloning from plain file directories not yet supported")
        manifestPath = writeMultiSpecManifest(
            destDir, manifestName, specRepo, None, sourceDir, templateVars
        )
        localEnv = LocalEnv(manifestPath, project=sourceProject)
        manifest = yamlmanifest.ReadOnlyManifest(localEnv=localEnv)
    else:
        # didn't find a template file
        # look for an ensemble at the given path or use the source project's default
        try:
            localEnv = LocalEnv(sourcePath, project=sourceProject)
            manifest = yamlmanifest.clone(localEnv, targetPath)
        except:
            return None  # can't find anything to clone
    return initNewEnsemble(manifest, sourceProject, targetProject)
    # XXX need to add manifest to unfurl.yaml


def cloneEnsembleFromDirectory(source, destDir):
    # source is not in a project, create a new project
    raise UnfurlError("Cloning from plain file directories not yet supported")
    # createProject(projectdir, home=None, mono=False, existing=False)

    # XXX if source is a remote git url:
    #       sourceProject = createNewProject(destDir)
    #       clone source into sourceProject

    # destRoot = Project.findPath(destDir)
    # if destRoot:
    #     destProject = Project(destRoot)
    # else:
    #     destProject = createNewProject(destDir, empty=True)
    # destDir = None  # use default names
    # # this will create clone the default ensemble if it doesn't point to a specific spec or ensemble
    # return createNewEnsemble(source, sourceProject, destDir, destProject)


def cloneEnsemble(source, destDir, includeLocal=False, **options):
    """
    Clone the `source` ensemble to `dest`. If `dest` isn't in a project, create one.
    `source` can point to an ensemble_template, a service_template, an existing ensemble
    or a folder containing one of those. If it points to a project its default ensemble will be cloned.

    Referenced `repositories` will be cloned if a git repository or copied if a regular file folder,
    If the folders already exist they will be copied to new folder unless the git repositories have the same HEAD.
    but the local repository names will remain the same.
    """
    # for each repository besides "self", clone if necessary and adjust

    sourceRoot = Project.findPath(source)
    if sourceRoot:
        sourceProject = Project(sourceRoot)
    else:
        return cloneEnsembleFromDirectory(source, destDir)

    newProject = False
    # if dest is in the source project:
    if sourceProject.isPathInProject(destDir):
        # add a new ensemble to the current project
        destProject = sourceProject
    else:
        destRoot = Project.findPath(destDir)
        if destRoot:
            destProject = Project(destRoot)
        else:
            # XXX use mono, existing options?
            newHome, projectConfigPath = createProject(
                destDir, addDefaults=False, **options
            )
            destProject = Project(projectConfigPath)
            newProject = True
            destDir = os.path.join(destDir, DefaultNames.EnsembleDirectory)

    # this will clone the default ensemble if it doesn't point to a specific spec or ensemble
    createNewEnsemble(source, sourceProject, destDir, destProject)
    return 'Created new ensemble "%s" in %s project at "%s"' % (
        destDir,
        "new" if newProject else "existing",
        os.path.abspath(destProject.projectRoot),
    )


def initEngine(projectDir, engine):
    kind, sep, rest = engine.partition(":")
    if kind == "venv":
        return createVenv(projectDir, rest)
    # elif kind == 'docker'
    # XXX return 'unrecoginized engine string: "%s"'
    return False


def _addUnfurlToVenv(projectdir):
    # this is hacky
    # can cause confusion if it exposes more packages than unfurl
    base = os.path.dirname(os.path.dirname(_templatePath))
    sitePackageDir = None
    libDir = os.path.join(projectdir, os.path.join(".venv", "lib"))
    for name in os.listdir(libDir):
        sitePackageDir = os.path.join(libDir, name, "site-packages")
        if os.path.isdir(sitePackageDir):
            break
    else:
        # XXX report error: can't find site-packages folder
        return
    _writeFile(sitePackageDir, "unfurl.pth", base)
    _writeFile(sitePackageDir, "unfurl.egg-link", base)


def createVenv(projectDir, pipfileLocation):
    """Create a virtual python environment for the given project."""
    os.environ["PIPENV_IGNORE_VIRTUALENVS"] = "1"
    os.environ["PIPENV_VENV_IN_PROJECT"] = "1"
    if "PIPENV_PYTHON" not in os.environ:
        os.environ["PIPENV_PYTHON"] = sys.executable

    try:
        cwd = os.getcwd()
        os.chdir(projectDir)
        # need to set env vars and change current dir before importing pipenv
        from pipenv.core import do_install, ensure_python
        from pipenv.utils import python_version

        pythonPath = str(ensure_python())
        assert pythonPath, pythonPath
        if not pipfileLocation:
            versionStr = python_version(pythonPath)
            assert versionStr, versionStr
            version = versionStr.rpartition(".")[0]  # 3.8.1 => 3.8
            # version = subprocess.run([pythonPath, "-V"]).stdout.decode()[
            #     7:10
            # ]  # e.g. Python 3.8.1 => 3.8
            pipfileLocation = os.path.join(
                _templatePath, "python" + version
            )  # e.g. templates/python3.8

        if not os.path.isdir(pipfileLocation):
            # XXX 'Pipfile location is not a valid directory: "%s" % pipfileLocation'
            return False

        # copy Pipfiles to project root
        if os.path.abspath(projectDir) != os.path.abspath(pipfileLocation):
            for filename in ["Pipfile", "Pipfile.lock"]:
                path = os.path.join(pipfileLocation, filename)
                if os.path.isfile(path):
                    shutil.copy(path, projectDir)

        # create the virtualenv and install the dependencies specified in the Pipefiles
        sys_exit = sys.exit
        try:
            retcode = -1

            def noexit(code):
                retcode = code

            sys.exit = noexit

            do_install(python=pythonPath)
            # this doesn't actually install the unfurl so link to this one
            _addUnfurlToVenv(projectDir)
        finally:
            sys.exit = sys_exit

        return not retcode  # retcode means error
    finally:
        os.chdir(cwd)
