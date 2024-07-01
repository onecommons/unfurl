import os
import shutil
import tarfile
import urllib.request
from appdirs import user_cache_dir
from ..logs import getLogger

logger = getLogger("unfurl.gui.assets_download")

TAG = "v0.1.0.alpha.1"
RELEASE = f"https://github.com/onecommons/unfurl-gui/releases/download/{TAG}/unfurl-gui-dist.tar.gz"
TAG_FILE = os.path.join(user_cache_dir(), 'unfurl_gui', 'current_tag.txt')
DIST = os.path.join(user_cache_dir(), 'unfurl_gui', 'dist')

def fetch():
    logger.debug(f"Checking assets for '{TAG}'")
    if 'UFGUI_DIR' in os.environ or 'WEBPACK_ORIGIN' in os.environ:
        return

    if os.path.exists(TAG_FILE):
        with open(TAG_FILE, 'r') as f:
            current_tag = f.read().strip()
        if current_tag == TAG and os.path.exists(DIST):
            logger.debug(f"'{TAG}' is up-to-date")
            return
        else:
            logger.debug(f"'{current_tag}' does not match the needed tag '{TAG}'")

    if os.path.exists(DIST):
        shutil.rmtree(DIST)
        logger.debug("Removed existing dist directory")

    logger.debug(f"Downloading {RELEASE}")
    os.makedirs(DIST, exist_ok=True)
    tar_path = os.path.join(DIST, 'unfurl-gui-dist.tar.gz')
    urllib.request.urlretrieve(RELEASE, tar_path)

    with tarfile.open(tar_path, 'r:gz') as tar:
        logger.debug(f"Extracting {RELEASE} to {DIST}")
        tar.extractall(path=os.path.dirname(DIST))

    os.remove(tar_path)
    logger.debug("Removed tarball file")

    with open(TAG_FILE, 'w') as f:
        f.write(TAG)
    logger.debug(f"Updated tag file to '{TAG}'")
