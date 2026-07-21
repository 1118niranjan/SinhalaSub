"""Double-click launcher: opens SinhalaSub with no console window (pythonw)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sinhalasub

sinhalasub.main()
