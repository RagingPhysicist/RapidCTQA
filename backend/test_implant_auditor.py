import sys
import os
from typing import Dict, Any

# Mock models before importing QAEngine
class QAFlag:
    def __init__(self, name, status, message):
        self.name = name
        self.status = status
        self.message = message
    def __repr__(self):
        return f"QAFlag(name='{self.name}', status='{self.status}', message='{self.message}')"

import yaml
from backend.engine import QAEngine

def test_implant_auditor_rules():
    # Setup mock engine with dummy config
    engine = QAEngine("ctqa.yaml")

    # Test Case 1: Only Internal Metal
    metrics_internal = {
        "metal_inside_cc": 0.5,
        "metal_outside_cc": 0.01,
        "metal_inside_slices": [1, 2, 3],
        "metal_outside_slices": [],
        "truncation_detected": False,
        "slice_spacing_var": 0.0,
        "monotonic_z": True,
        "gantry_tilt": 0.0,
        "duplicate_slices": False,
        "background_air_sd": 5.0,
        "air_hu_estimate": -1000.0,
        "fluid_median_hu": 15.0,
        "rescale_slope": 1.0,
        "gas_volume_cc": 0.0,
        "max_tilt_deg": 0.0,
        "pediatric_mismatch": False,
        "slice_count": 10,
        "slice_thickness": 2.0
    }

    flags = engine._evaluate_rules(metrics_internal)

    internal_flags = [f for f in flags if "INTERNAL_METAL" in f.message]
    external_flags = [f for f in flags if "EXTERNAL_METAL" in f.message]

    assert len(internal_flags) == 1, f"Expected 1 internal metal flag, got {len(internal_flags)}"
    assert "Slices 1-3" in internal_flags[0].message
    assert len(external_flags) == 0, f"Expected 0 external metal flags, got {len(external_flags)}"

    print("Test Case 1 (Internal Only) Passed")

    # Test Case 2: Only External Metal
    metrics_external = metrics_internal.copy()
    metrics_external["metal_inside_cc"] = 0.0
    metrics_external["metal_outside_cc"] = 0.8
    metrics_external["metal_outside_slices"] = [5, 6]

    flags = engine._evaluate_rules(metrics_external)

    internal_flags = [f for f in flags if "INTERNAL_METAL" in f.message]
    external_flags = [f for f in flags if "EXTERNAL_METAL" in f.message]

    assert len(internal_flags) == 0
    assert len(external_flags) == 1
    assert "Slices 5-6" in external_flags[0].message

    print("Test Case 2 (External Only) Passed")

    # Test Case 3: Both Internal and External Metal
    metrics_both = metrics_internal.copy()
    metrics_both["metal_inside_cc"] = 0.5
    metrics_both["metal_outside_cc"] = 0.8
    metrics_both["metal_inside_slices"] = [1]
    metrics_both["metal_outside_slices"] = [10]

    flags = engine._evaluate_rules(metrics_both)

    internal_flags = [f for f in flags if "INTERNAL_METAL" in f.message]
    external_flags = [f for f in flags if "EXTERNAL_METAL" in f.message]

    assert len(internal_flags) == 1
    assert len(external_flags) == 1
    assert "Slice 1" in internal_flags[0].message
    assert "Slice 10" in external_flags[0].message

    print("Test Case 3 (Both) Passed")

    # Test Case 4: Below Threshold
    metrics_low = metrics_internal.copy()
    metrics_low["metal_inside_cc"] = 0.02 # Below default 0.05
    metrics_low["metal_outside_cc"] = 0.01

    flags = engine._evaluate_rules(metrics_low)

    implant_flags = [f for f in flags if f.name == "ImplantAuditor"]
    assert len(implant_flags) == 0

    print("Test Case 4 (Below Threshold) Passed")

if __name__ == "__main__":
    try:
        test_implant_auditor_rules()
        print("\nAll ImplantAuditor rule tests passed!")
    except AssertionError as e:
        print(f"\nTest failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\nAn error occurred: {e}")
        traceback.print_exc()
        sys.exit(1)
