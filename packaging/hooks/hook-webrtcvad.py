"""Use metadata from webrtcvad-wheels, which supplies the webrtcvad module."""
from PyInstaller.utils.hooks import copy_metadata

datas = copy_metadata("webrtcvad-wheels")
