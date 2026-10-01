from dataclasses import dataclass, field
from typing import Iterable, List, Tuple

import numpy as np
import pydicom

from backend.qa_config import Thresholds


@dataclass
class SeriesContext:
    """Everything the agents need about one CT series, computed once."""
    datasets: List[pydicom.Dataset]
    hu_volume: np.ndarray
    protocol: str
    pixel_spacing: Tuple[float, float]
    voxel_vol_cc: float
    interior_mask: np.ndarray          # filled patient body (no couch / accessories)
    accessory_table_mask: np.ndarray   # couch, wingboard, vac-bag, immobilisers
    empty_slices: List[int]            # 1-indexed slices with no patient voxels
    thresholds: Thresholds
    used_totalsegmentator: bool = False  # body mask from TotalSegmentator, not the rule-based one
    study_desc: str = field(init=False)
    body_part: str = field(init=False)

    def __post_init__(self):
        first = self.datasets[0]
        self.study_desc = str(getattr(first, 'StudyDescription', ''))
        self.body_part = str(getattr(first, 'BodyPartExamined', ''))


def mentions_any(keywords: Iterable[str], *texts: str) -> bool:
    """Case-insensitive keyword match against any of the given strings."""
    haystacks = [t.upper() for t in texts]
    return any(kw.upper() in h for kw in keywords for h in haystacks)


def format_slices(slices: List[int]) -> str:
    """Compact, human-readable slice list for flag messages, e.g. ' (Slices 1-3, 5)'."""
    if not slices:
        return ""
    if len(slices) == 1:
        return f" (Slice {slices[0]})"

    slices = sorted(set(slices))
    ranges = []
    start = end = slices[0]
    for s in slices[1:]:
        if s == end + 1:
            end = s
        else:
            ranges.append(f"{start}" if start == end else f"{start}-{end}")
            start = end = s
    ranges.append(f"{start}" if start == end else f"{start}-{end}")
    return f" (Slices {', '.join(ranges)})"
