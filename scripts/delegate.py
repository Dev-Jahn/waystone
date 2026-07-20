#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Compatibility adapter for the delegation runtime."""
from __future__ import annotations

import sys
from pathlib import Path

_repo_root = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _repo_root)
_waystone_preloaded = "waystone" in sys.modules
try:
    import waystone.runs.delegate as _delegate_owner
finally:
    sys.path.remove(_repo_root)

for _name, _value in vars(_delegate_owner).items():
    if not _name.startswith("__"):
        globals()[_name] = _value
__doc__ = _delegate_owner.__doc__


class _DelegateShim(type(sys)):
    """Keep legacy module-level monkeypatches bound to the moved runtime's globals."""

    _routes = {
        name: (_delegate_owner,)
        for name, value in vars(sys.modules[__name__]).items()
        if name in vars(_delegate_owner) and vars(_delegate_owner)[name] is value
    }

    def __setattr__(self, name, value):
        for owner in self.__class__._routes.get(name, ()):
            setattr(owner, name, value)
        super().__setattr__(name, value)

    def __delattr__(self, name):
        for owner in self.__class__._routes.get(name, ()):
            delattr(owner, name)
        super().__delattr__(name)


if not _waystone_preloaded:
    del sys.modules["waystone"]
sys.modules[__name__].__class__ = _DelegateShim
del _DelegateShim, _delegate_owner, _name, _repo_root, _value, _waystone_preloaded


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
