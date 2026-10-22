"""Set the system volume (used when the user reports they cannot hear Jarvis)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.skills import system


def main() -> int:
    target = int(sys.argv[1]) if len(sys.argv) > 1 else 70
    print("before:", system.get_volume())
    print(system.set_volume(target))
    print("after :", system.get_volume())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
