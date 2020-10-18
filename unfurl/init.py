"""
Creates and clone projects and ensembles.

After running "init" your Unfurl project will look like:

ensemble/ensemble.yaml
ensemble-template.yaml
unfurl.yaml
local/unfurl.yaml

If the --existing option is used, the project will be added to the nearest repository found in a parent folder.
If the --mono option is used, the ensemble add the project repo instead of it's own.

Each repository created will also have .gitignore and .gitattributes added.

When a repository is added as child of another repo, that folder will be added to .git/info/exclude
(instead of .gitignore because they shouldn't be committed into the repository).

Paths are always relative but you can optionally specify which repository a path is relative to.

There are three predefined repositories:

"self", which represents the location the ensemble lives in -- it will be
a "git-local:" URL or a "file:" URL if the ensemble is not part of a git repository.

"unfurl" which points to the Python package of the unfurl process -- this can be used to load configurators and templates
that ship with Unfurl.

"spec" which, unless otherwise specified, points to the project root or the ensemble itself if it is not part of a project.
"""
import uuid
import os
import os.path
import datetime
import sys
import shutil
from . import DefaultNames, getHomeConfigPath
from .tosca import TOSCA_VERSION
from .repo import Repo, GitRepo, splitGitUrl, isURLorGitPath
from .util import UnfurlError, getBaseDir
from .localenv import LocalEnv, Project
import random
import string

_templatePath = os.path.join(os.path.abspath(os.path.dirname(__file__)), "templates")


def renameForBackup(dir):
    ctime = datetime.datetime.fromtimestamp(os.stat(dir).st_ctime)
    new = dir + "." + ctime.strftime("%Y-%m-%d-%H-%M-%S")
    os.rename(dir, new)
    return new


def get_random_password(count=12, prefix="uv"):
    srandom = random.SystemRandom()
    start = string.ascii_letters + string.digits
    source = string.ascii_letters + string.digits + "%&()*+,-./:<>?=@[]^_`{}~"
    return prefix + "".join(
        srandom.choice(source if i else start) for i in range(count)
    )


def _writeFile(folder, filename, content):
    if not os.path.isdir(folder):
        os.makedirs(folder)
    filepath = os.path.join(folder, filename)
    with open(filepath, "w") as f:
        f.write(content)
    return filepath


def writeTemplate(folder, filename, template, vars, templateDir=None):
    from .runtime import NodeInstance
    from .eval import RefContext
    from .support import applyTemplate

    if templateDir and not os.path.isabs(templateDir):
        templateDir = os.path.join(_templatePath, templateDir)
    if not templateDir or not os.path.exists(os.path.join(templateDir, template)):
        templateDir = _templatePath  # default

    with open(os.path.join(templateDir, template)) as f:
        source = f.read()
    instance = NodeInstance()
    instance._baseDir = _templatePath
    content = applyTemplate(source, RefContext(instance, vars))
    return _writeFile(folder, filename, content)


def writeProjectConfig(
    projectdir,
    filename=DefaultNames.LocalConfig,
    templatePath=DefaultNames.LocalConfig + ".j2",
    vars=None,
    templateDir=None,
):
    _vars = dict(include="", manifestPath=None)
    if vars:
        _vars.update(vars)
    return writeTemplate(projectdir, filename, templatePath, _vars, templateDir)


def renderHome(homePath):
    homedir, filename = os.path.split(homePath)
    writeTemplate(homedir, DefaultNames.Ensemble, "manifest.yaml.j2", {}, "home")
    configPath = writeProjectConfig(
        homedir, filename, "unfurl.yaml.j2", templateDir="home"
    )
    writeTemplate(homedir, ".gitignore", "gitignore.j2", {})
    return configPath


def createHome(home=None, render=False, replace=False, **kw):
    """
    Create the home project if missing
    """
    homePath = getHomeConfigPath(home)
    if not homePath:
        return None
    exists = os.path.exists(homePath)
    if exists and not replace:
        return None

    homedir, filename = os.path.split(homePath)
    if render:  # just render
        repo = None
    else:
        if exists:
            renameForBackup(homedir)
        repo = _createRepo(homedir)

    configPath = renderHome(homePath)
    if not render and not kw.get("no_runtime"):
        initEngine(homedir, kw.get("runtime") or "venv:")
    if repo:
        createProjectRepo(homedir, repo, True, addDefaults=False, templateDir="home")
        repo.repo.git.add("--all")
        repo.repo.index.commit("Create the unfurl home repository")
        repo.repo.git.branch("rendered")  # now create a branch
    return configPath


def _createRepo(gitDir, gitUri=None):
    import git

    if not os.path.isdir(gitDir):
        os.makedirs(gitDir)
    repo = git.Repo.init(gitDir)
    repo.index.add(addHiddenGitFiles(gitDir))
    repo.index.commit("Initial Commit for %s" % uuid.uuid1())

    Repo.ignoreDir(gitDir)
    return GitRepo(repo)


def writeServiceTemplate(projectdir):
    vars = dict(version=TOSCA_VERSION)
    return writeTemplate(
        projectdir, "service-template.yaml", "service-template.yaml.j2", vars
    )


def writeEnsembleManifest(
    destDir, manifestName, specRepo, specDir=None, extraVars=None
):
    if extraVars is None:
        # default behaviour is to include the ensembleTemplate
        # in the root of the specDir
        extraVars = dict(ensembleTemplate=DefaultNames.EnsembleTemplate)

    if specDir:
        specDir = os.path.abspath(specDir)
    else:
        specDir = ""
    vars = dict(specRepoUrl=specRepo.getGitLocalUrl(specDir, "spec"))
    vars.update(extraVars)
    return writeTemplate(destDir, manifestName, "manifest.yaml.j2", vars)


def addHiddenGitFiles(gitDir):
    # write .gitignore and  .gitattributes
    gitIgnorePath = writeTemplate(gitDir, ".gitignore", "gitignore.j2", {})
    gitAttributesContent = "**/*%s merge=union\n" % DefaultNames.JobsLog
    gitAttributesPath = _writeFile(gitDir, ".gitattributes", gitAttributesContent)
    return [os.path.abspath(gitIgnorePath), os.path.abspath(gitAttributesPath)]


def createProjectRepo(
    projectdir,
    repo,
    mono,
    addDefaults=True,
    projectConfigTemplate=None,
    templateDir=None,
):
    """
    Creates a folder named `projectdir` with a git repository with the following files:

    unfurl.yaml
    local/unfurl.yaml
    ensemble-template.yaml
    ensemble/ensemble.yaml

    Returns the absolute path to unfurl.yaml
    """
    # write the project files
    localConfigFilename = DefaultNames.LocalConfig
    manifestPath = os.path.join(DefaultNames.EnsembleDirectory, DefaultNames.Ensemble)

    vars = dict(vaultpass=get_random_password())
    # manifestPath should be in local if ensemble is a separate repo
    if addDefaults and not mono:
        vars["manifestPath"] = manifestPath
    writeProjectConfig(
        os.path.join(projectdir, "local"),
        localConfigFilename,
        "unfurl.local.yaml.j2",
        vars,
        templateDir,
    )

    localInclude = "+?include: " + os.path.join("local", localConfigFilename)
    vars = dict(include=localInclude)
    # manifestPath should here if ensemble is part of the repo
    if addDefaults and mono:
        vars["manifestPath"] = manifestPath
    projectConfigPath = writeProjectConfig(
        projectdir,
        DefaultNames.LocalConfig,
        projectConfigTemplate or DefaultNames.LocalConfig + ".j2",
        vars,
        templateDir,
    )
    files = [projectConfigPath]

    if addDefaults:
        # write ensemble-template.yaml
        ensembleTemplatePath = writeTemplate(
            projectdir, DefaultNames.EnsembleTemplate, "manifest-template.yaml.j2", {}
        )
        files.append(ensembleTemplatePath)
        ensembleDir = os.path.join(projectdir, DefaultNames.EnsembleDirectory)
        manifestName = DefaultNames.Ensemble
        if mono:
            ensembleRepo = repo
        else:
            ensembleRepo = _createRepo(ensembleDir)
        extraVars = dict(
            ensembleUri=ensembleRepo.getUrlWithPath(
                os.path.abspath(os.path.join(ensembleDir, manifestName))
            )
        )
        # write ensemble/ensemble.yaml
        manifestPath = writeEnsembleManifest(
            ensembleDir, manifestName, repo, extraVars=extraVars
        )
        if mono:
            files.append(manifestPath)
        else:
            ensembleRepo.repo.index.add([os.path.abspath(manifestPath)])
            ensembleRepo.repo.index.commit("Default ensemble repository boilerplate")

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

    if repo:
        repo.repo.index.add(addHiddenGitFiles(projectdir))
        repo.repo.index.commit("Adding Unfurl project")
    else:
        repo = _createRepo(projectdir)

    # XXX add project to ~/.unfurl_home/unfurl.yaml
    projectConfigPath = createProjectRepo(
        projectdir, repo, mono, addDefaults=addDefaults
    )

    if not newHome and not kw.get("no_runtime") and kw.get("runtime"):
        # if runtime was explicitly set and we aren't creating the home project
        # then initialize the runtime here
        initEngine(projectdir, kw.get("runtime"))

    return newHome, projectConfigPath


def cloneLocalRepos(manifest, sourceProject, targetProject):
    # We need to clone repositories that are local to the source project
    for repoSpec in manifest.repositories.values():
        if repoSpec.name == "self":
            continue
        repo = sourceProject.findRepository(repoSpec)
        if repo:
            targetProject.findOrClone(repo)


def initNewEnsemble(manifest, sourceProject, targetProject):
    cloneLocalRepos(manifest, sourceProject, targetProject)

    destDir = os.path.dirname(manifest.manifest.path)
    if targetProject.projectRepo and targetProject is not sourceProject:
        repo = targetProject.projectRepo
    else:
        # XXX support --mono and --existing
        repo = _createRepo(destDir)

    manifest.metadata["uri"] = repo.getUrlWithPath(manifest.manifest.path)
    with open(manifest.manifest.path, "w") as f:
        manifest.dump(f)

    repo.repo.index.add([manifest.manifest.path])
    repo.repo.index.commit("Default ensemble repository boilerplate")
    return repo


def _looksLike(path, name):
    # in case path is a directory:
    if os.path.isfile(os.path.join(path, name)):
        return path, name
    if path.endswith(name):  # name is explicit so don't need to check if file exists
        return os.path.split(path)
    return None


def _getEnsemblePaths(sourcePath, sourceProject):
    # look for an ensemble-template or service-template in source path
    template = _looksLike(sourcePath, DefaultNames.EnsembleTemplate)
    if template:
        return dict(sourceDir=template[0], ensembleTemplate=template[1])
    template = _looksLike(sourcePath, DefaultNames.ServiceTemplate)
    if template:
        return dict(sourceDir=template[0], serviceTemplate=template[1])
    else:
        # we couldn't find one of the default template files, so treat sourcePath
        # as a path to an ensemble
        # XXX if sourcePath points to an explicit file we should check if it's a service template first
        try:
            localEnv = LocalEnv(sourcePath, project=sourceProject)
            return dict(manifestPath=localEnv.manifestPath, localEnv=localEnv)
        except:
            # nothing found
            return {}


def createNewEnsemble(templateVars, sourceProject, targetPath, targetProject):
    """
    include directives, imports, or external file reference they are guaranteed to be local to the project.
    Outside paths need to be referenced with a named repository.
    """
    # if targetPath is relative, it will be treated as relative to the targetProject root
    from unfurl import yamlmanifest

    if not targetPath:
        destDir, manifestName = DefaultNames.EnsembleDirectory, DefaultNames.Ensemble
        targetPath = os.path.join(destDir, manifestName)
    elif targetPath.endswith(".yaml") or targetPath.endswith(".yml"):
        destDir, manifestName = os.path.split(targetPath)
    else:
        destDir = targetPath
        manifestName = DefaultNames.Ensemble
    # choose a destDir that doesn't conflict with an existing folder
    # (i.e. if default ensemble already exists)
    destDir = targetProject.getUniquePath(destDir)
    targetPath = os.path.join(destDir, manifestName)

    if "manifestPath" not in templateVars:
        # we found a template file to clone
        assert sourceProject
        sourceDir = templateVars["sourceDir"]
        specRepo, relPath, revision, bare = sourceProject.findPathInRepos(sourceDir)
        if not specRepo:
            raise UnfurlError(
                '"%s" is not in a git repository. Cloning from plain file directories not yet supported'
                % sourceDir
            )
        manifestPath = writeEnsembleManifest(
            destDir, manifestName, specRepo, sourceDir, templateVars
        )
        localEnv = LocalEnv(manifestPath, project=sourceProject)
        manifest = yamlmanifest.ReadOnlyManifest(localEnv=localEnv)
    elif templateVars:
        # didn't find a template file
        # look for an ensemble at the given path or use the source project's default
        manifest = yamlmanifest.clone(templateVars["localEnv"], targetPath)
    else:
        return None  # can't find anything to clone
    repo = initNewEnsemble(manifest, sourceProject, targetProject)
    return destDir, repo
    # XXX need to add manifest to unfurl.yaml


def getSourceProject(source, destDir):
    # check if source is a git url
    if isURLorGitPath(source):
        repoURL, filePath, revision = splitGitUrl(source)
        # if destProject add repo to project
        destRoot = Project.findPath(destDir)
        if destRoot:
            # destination is in an existing project, use that one
            sourceProject = Project(destRoot)
            repo = sourceProject.findGitRepo(repoURL, revision)
            if not repo:
                repo = sourceProject.createWorkingDir(repoURL, revision)
            source = os.path.join(repo.workingDir, filePath)
        else:
            # otherwise clone to dest
            Repo.createWorkingDir(repoURL, destDir, revision)
            targetDir = os.path.join(destDir, filePath)
            sourceRoot = Project.findPath(targetDir)
            if sourceRoot:
                sourceProject = Project(sourceRoot)
                source = targetDir
                # we cloned the repo to destDir but the might not be at it's root
                destDir = sourceProject.projectRoot
            else:
                return (
                    None,
                    None,
                    None,
                    (
                        'Error: cloned "%s" to "%s" but couldn\'t find an Unfurl project'
                        % (source, destDir)
                    ),
                )
    else:
        sourceRoot = Project.findPath(source)
        if sourceRoot:
            sourceProject = Project(sourceRoot)
        else:
            # XXX support cloning from an ensemble or template that isn't in a project
            return (
                None,
                None,
                None,
                (
                    "Can't clone \"%s\": it isn't in an Unfurl project or repository"
                    % source
                ),
            )
    return sourceProject, source, destDir, None


def cloneEnsemble(source, destDir, includeLocal=False, **options):
    """
    Clone the `source` ensemble to `dest`. If `dest` isn't in a project, create one.
    `source` can point to an ensemble_template, a service_template, an existing ensemble
    or a folder containing one of those. If it points to a project its default ensemble will be cloned.

    Referenced `repositories` will be cloned if a git repository or copied if a regular file folder,
    If the folders already exist they will be copied to new folder unless the git repositories have the same HEAD.
    but the local repository names will remain the same.
    """
    from unfurl import yamlmanifest

    sourceProject, source, destDir, error = getSourceProject(source, destDir)
    if error:
        return error

    newProject = False
    paths = _getEnsemblePaths(source, sourceProject)
    relDestDir = sourceProject.getRelativePath(destDir)
    # if dest is in the source project:
    if not relDestDir.startswith(".."):
        # add a new ensemble to the current project
        destProject = sourceProject
        destDir = "" if relDestDir == "." else relDestDir
    else:
        destRoot = Project.findPath(destDir)
        if destRoot:
            # destination is in an existing project, use that one
            destProject = Project(destRoot)
        else:  # create new project
            newProject = True
            destProject = None
            # if source in the sourceProject's projectRepo, clone that repo
            # instead of creating a new project repo
            if sourceProject.projectRepo:
                # check if source points to an ensemble that is part of the project repo
                # if source is root, we need to get the default ensemble
                pathToEnsemble = paths.get("manifestPath")
                if pathToEnsemble:
                    srcInRepo = (
                        sourceProject.projectRepo.findRepoPath(pathToEnsemble)
                        is not None
                    )
                    if srcInRepo:
                        # if source is an ensemble and it's in the project repo
                        # we've cloned the ensemble already so we're done
                        sourceProject.projectRepo.clone(destDir)
                        destProject = Project(Project.findPath(destDir))
                        manifest = yamlmanifest.ReadOnlyManifest(
                            localEnv=paths["localEnv"]
                        )
                        cloneLocalRepos(manifest, sourceProject, destProject)
                        return "Cloned project to " + os.path.abspath(destDir)
                    # XXX
                    # else:
                    #     # the source ensemble is not in the project repo
                    #     # if one of the repositories it references is the project repo, clone it
                    #     specFolder = getSpecFoldersFromEnsemble(pathToEnsemble)
                    #     specInRepo = (
                    #         sourceProject.projectRepo.findRepoPath(specFolder)
                    #         is not None
                    #     )
                    #     if specInRepo:
                    #         sourceProject.projectRepo.clone(destDir)
                    #         destProject = Project(destDir)

                # if source is at root of the project repo, just clone it
                if os.path.normpath(sourceProject.projectRepo.workingDir) == getBaseDir(
                    source
                ):
                    # clone the project but we still need to add a new ensemble to it
                    destDir = os.path.abspath(destDir)
                    sourceProject.projectRepo.clone(destDir)
                    # print(
                    #    "cloning", sourceProject.projectRepo.workingDir, "to", destDir
                    # )
                    destProject = Project(Project.findPath(destDir))
                    destDir = DefaultNames.EnsembleDirectory

            if not destProject:
                # create a new project from scratch for the new ensemble
                newHome, projectConfigPath = createProject(
                    destDir, addDefaults=False, **options
                )
                destProject = Project(projectConfigPath)
                destDir = DefaultNames.EnsembleDirectory

    # this will clone the default ensemble if it doesn't point to a specific spec or ensemble
    destDir, repo = createNewEnsemble(paths, sourceProject, destDir, destProject)
    return 'Created new ensemble "%s" in %s project at "%s"' % (
        destDir,
        "new" if newProject else "existing",
        os.path.abspath(destProject.projectRoot),
    )


def initEngine(projectDir, runtime):
    kind, sep, rest = runtime.partition(":")
    if kind == "venv":
        return createVenv(projectDir, rest)
    # elif kind == 'docker'
    # XXX return 'unrecoginized runtime string: "%s"'
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
