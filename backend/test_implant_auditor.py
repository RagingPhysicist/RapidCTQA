import sys
import traceback

# Mock models before importing QAEngine
class QAFlag:
    def __init__(self, name, status, message):
        self.name = name
        self.status = status
        self.message = message
    def __repr__(self):
        return f"QAFlag(name='{self.name}', status='{self.status}', message='{self.message}')"

from backend.engine import QAEngine

def test_implant_auditor_rules_refined():
    # Setup mock engine with dummy config
    engine = QAEngine("ctqa.yaml")

    # Test Case 1: Internal, Surface and External Metal
    metrics = {
        "metal_internal_cc": 0.5,
        "metal_surface_cc": 0.3,
        "metal_external_cc": 0.8,
        "metal_internal_slices": [1],
        "metal_surface_slices": [2],
        "metal_external_slices": [3],
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

    flags = engine._evaluate_rules(metrics)

    internal_flags = [f for f in flags if "INTERNAL_METAL" in f.message]
    surface_flags = [f for f in flags if "SURFACE_METAL" in f.message]
    external_flags = [f for f in flags if "EXTERNAL_METAL" in f.message]

    assert len(internal_flags) == 1
    assert "detected deep inside body" in internal_flags[0].message
    assert "Slice 1" in internal_flags[0].message

    assert len(surface_flags) == 1
    assert "detected on patient skin/surface" in surface_flags[0].message
    assert "Slice 2" in surface_flags[0].message

    assert len(external_flags) == 1
    assert "detected outside body" in external_flags[0].message
    assert "Slice 3" in external_flags[0].message

    print("Test Case 1 (All categories) Passed")

    # Test Case 2: Below Threshold
    metrics_low = metrics.copy()
    metrics_low["metal_internal_cc"] = 0.01
    metrics_low["metal_surface_cc"] = 0.02
    metrics_low["metal_external_cc"] = 0.03

    flags = engine._evaluate_rules(metrics_low)

    implant_flags = [f for f in flags if f.name == "ImplantAuditor"]
    assert len(implant_flags) == 0

    print("Test Case 2 (Below Threshold) Passed")

if __name__ == "__main__":
    try:
        test_implant_auditor_rules_refined()
        print("\nAll Refined ImplantAuditor rule tests passed!")
    except AssertionError as e:
        print(f"\nTest failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\nAn error occurred: {e}")
        traceback.print_exc()
        sys.exit(1)
