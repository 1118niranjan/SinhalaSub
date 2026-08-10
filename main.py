"""SinhalaSub launcher.

Run with:  python main.py
(or double-click SinhalaSub.pyw to start it without a console window)

The application code lives in the `sinhalasub/` folder next to this file.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sinhalasub.app import main  # noqa: E402

if __name__ == "__main__":
    main()
