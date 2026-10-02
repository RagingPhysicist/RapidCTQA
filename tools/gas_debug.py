"""Print CavityScout gas components for one series, to tune the thresholds in
ctqa.yaml (thresholds.gas) on real cases.

Usage (from the repository root):
    python tools/gas_debug.py <series_dir> [--protocol "RTP Pelvis"] [--totalsegmentator]

Read-only: nothing is written next to the series, and nothing is logged,
exported or sent. By default the rule-based body mask is used; pass
--totalsegmentator to use (and if needed run) the TotalSegmentator body mask.
"""
import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("series_dir", help="folder with the series' .dcm files")
    parser.add_argument("--protocol", help="override ProtocolName (e.g. to force a pelvis/abdomen protocol)")
    parser.add_argument("--totalsegmentator", action="store_true", help="use the TotalSegmentator body mask")
    parser.add_argument("--config", default=None, help="ctqa.yaml to use (default: repository ctqa.yaml)")
    args = parser.parse_args(argv)

    from backend.engine import DISABLE_TOTALSEGMENTATOR_ENV, QAEngine
    if not args.totalsegmentator:
        os.environ[DISABLE_TOTALSEGMENTATOR_ENV] = "1"

    import pydicom
    from backend.engine import CT_IMAGE_STORAGE
    from backend.settings import QA_CONFIG_PATH

    files = sorted(glob.glob(os.path.join(args.series_dir, "*.dcm")))
    datasets = [ds for ds in (pydicom.dcmread(f) for f in files) if getattr(ds, "SOPClassUID", "") == CT_IMAGE_STORAGE]
    if not datasets:
        sys.exit(f"No CT images found in {args.series_dir}")
    datasets.sort(key=lambda ds: float(ds.ImagePositionPatient[2]))
    protocol = args.protocol or str(getattr(datasets[0], "ProtocolName", "Unknown"))

    segmentation_service = None
    if args.totalsegmentator:
        from backend.segmentation import SegmentationService
        segmentation_service = SegmentationService(storage_dir=os.path.dirname(os.path.abspath(args.series_dir)))
    engine = QAEngine(args.config or QA_CONFIG_PATH, segmentation_service=segmentation_service)

    m = engine._compute_metrics(datasets, protocol=protocol)
    cfg = engine.thresholds_for(protocol).gas
    print(f"Series:   {datasets[0].SeriesInstanceUID}  ({len(datasets)} slices)")
    print(f"Protocol: {protocol}   body mask: {'TotalSegmentator' if m.get('used_totalsegmentator') else 'rule-based'}")
    if not m.get("is_pelvis_or_abdomen_scan"):
        print("Not a pelvis/abdomen protocol: gas is not evaluated (use --protocol to force).")
        return
    print(f"Thresholds: air < {cfg.air_threshold_hu:g} HU, thickness >= {cfg.min_thickness_mm:g} mm, "
          f"depth >= {cfg.min_depth_mm:g} mm, cleft fraction < {cfg.cleft_fraction:g}")
    print(f"Gas kept: {m['gas_volume_cc']:.2f} cc   rejected: {m['gas_rejected_cc']:.2f} cc   "
          f"components: {m['gas_component_count']} (largest {len(m['gas_components'])} listed)")
    print(f"Gas slices: {m['gas_slices']}")
    print()
    print(f"{'volume cc':>10} {'slice':>6} {'y':>6} {'x':>6} {'depth mm':>9} {'thick mm':>9}  reason")
    for c in m["gas_components"]:
        ctr = c["centroid"]
        print(f"{c['volume_cc']:>10.3f} {ctr['slice']:>6} {ctr['y']:>6} {ctr['x']:>6} "
              f"{c['depth_mm']:>9.1f} {c['thickness_mm']:>9.1f}  {c['reason']}")


if __name__ == "__main__":
    main()
