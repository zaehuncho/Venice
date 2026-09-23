"""Check the specific YOLO pickup behavior on a source file or rollback copy."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys


path = Path(sys.argv[1]).resolve()
expected = sys.argv[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
spec = importlib.util.spec_from_file_location("pickup_state_reader", path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
reader = module.SimpleMeterReader.__new__(module.SimpleMeterReader)
base = SimpleNamespace()
reader._meter_detector = SimpleNamespace(_base=base)
reader._tracking_meter_style = "pill"
reader._open_pickup_record(71)
actual = ("record_open" if getattr(base, "pickup", {}).get("epoch") == 71
          else "no_op")
assert actual == expected, (actual, expected)
print(f"YOLO_PICKUP={actual}")
