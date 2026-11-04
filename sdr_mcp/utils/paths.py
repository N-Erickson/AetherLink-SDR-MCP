"""
Filesystem locations used by AetherLink
"""

import os

DEFAULT_RECORDINGS_DIR = os.path.join(os.path.expanduser("~"), "AetherLink", "recordings")


def recordings_dir() -> str:
    """Directory for recordings and decoder output.

    Defaults to ~/AetherLink/recordings (a temp directory would be purged by the
    OS). Override with the AETHERLINK_RECORDINGS_DIR environment variable.
    The directory is not created here; callers create it when they write.
    """
    return os.environ.get("AETHERLINK_RECORDINGS_DIR") or DEFAULT_RECORDINGS_DIR
