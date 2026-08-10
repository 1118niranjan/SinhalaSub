"""SinhalaSub - translate movie subtitles from any language into Sinhala.

The code lives in this package; the project root holds the launcher, your
settings and the translation database. ROOT_DIR points at that root so user
data is written beside the app rather than inside the source folder.
"""

import os

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(PACKAGE_DIR)

__version__ = "1.0.0"
__author__ = "NLK"
