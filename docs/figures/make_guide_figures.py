"""Generate every figure for the AnaTele illustrated guide.

Run from the repository root:  python docs/figures/make_guide_figures.py [name-filter ...]
Figures are written to docs/images/guide/.
"""
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

MODULES = ("figs_basics", "figs_phases", "figs_detectors", "figs_missions")


def main(argv=None):
    filters = list(argv if argv is not None else sys.argv[1:])
    for module_name in MODULES:
        module = importlib.import_module(module_name)
        for name, func in module.FIGURES:
            if not filters or any(f in name for f in filters):
                func()


if __name__ == "__main__":
    main()
