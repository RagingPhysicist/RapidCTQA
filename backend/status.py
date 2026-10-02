"""Single source of truth for QA status values.

Verdicts (per flag and per series):
    ACCEPT       - check passed / series may proceed to contouring
    CONDITIONAL  - clinician review required before use
    REJECT       - series must not be used (rescan / resend)

Informational / lifecycle values:
    INFO         - finding reported with its measured value, not actionable
                   (flag level only, never affects the verdict)
    SKIPPED      - check not applicable (flag level only, never affects the verdict)
    PENDING      - series received but not analysed yet (dashboard only)
    INGESTING    - series still arriving over DICOM (dashboard only)

Older builds wrote PASS / PASS_WITH_WARNING / FAIL_CRITICAL. Those values may
still exist in persisted ``qa_result.json`` files and daily logs, so every
reader goes through :func:`normalize_status`.
"""
from enum import Enum
from typing import Iterable, Optional


class QAStatus(str, Enum):
    ACCEPT = "ACCEPT"
    CONDITIONAL = "CONDITIONAL"
    REJECT = "REJECT"
    INFO = "INFO"
    SKIPPED = "SKIPPED"
    PENDING = "PENDING"
    INGESTING = "INGESTING"

    def __str__(self) -> str:
        return self.value


LEGACY_ALIASES = {
    "PASS": QAStatus.ACCEPT,
    "PASS_WITH_WARNING": QAStatus.CONDITIONAL,
    "FAIL_CRITICAL": QAStatus.REJECT,
}

# Lower number = more severe. Used for sorting the dashboard and for
# aggregating 4DCT phase statuses (worst phase wins).
SEVERITY = {
    QAStatus.REJECT: 0,
    QAStatus.CONDITIONAL: 1,
    QAStatus.INFO: 2,
    QAStatus.ACCEPT: 3,
    QAStatus.SKIPPED: 4,
    QAStatus.PENDING: 5,
    QAStatus.INGESTING: 6,
}

# Flag statuses that need a clinician's attention. Only these escalate the
# series verdict and only results containing them go to the problem log.
ACTIONABLE = frozenset({QAStatus.REJECT, QAStatus.CONDITIONAL})


def is_actionable(value) -> bool:
    return try_normalize_status(value) in ACTIONABLE


def normalize_status(value) -> QAStatus:
    """Map any current or legacy status string onto :class:`QAStatus`.

    Raises ``ValueError`` for unknown values so typos fail loudly instead of
    silently being treated as a pass.
    """
    if isinstance(value, QAStatus):
        return value
    key = str(value).strip().upper()
    if key in LEGACY_ALIASES:
        return LEGACY_ALIASES[key]
    return QAStatus(key)


def try_normalize_status(value) -> Optional[QAStatus]:
    """Like :func:`normalize_status` but returns ``None`` for unknown values."""
    try:
        return normalize_status(value)
    except ValueError:
        return None


def severity(value) -> int:
    status = try_normalize_status(value)
    return SEVERITY.get(status, 99)


def worst_status(values: Iterable, default: QAStatus = QAStatus.PENDING) -> QAStatus:
    statuses = [normalize_status(v) for v in values]
    if not statuses:
        return default
    return min(statuses, key=lambda s: SEVERITY[s])


def series_verdict(flag_statuses: Iterable) -> QAStatus:
    """Overall series verdict from its flag statuses.

    REJECT if any flag rejects, otherwise CONDITIONAL if any flag needs
    review, otherwise ACCEPT. INFO and SKIPPED flags never influence it.
    """
    statuses = {normalize_status(s) for s in flag_statuses}
    if QAStatus.REJECT in statuses:
        return QAStatus.REJECT
    if QAStatus.CONDITIONAL in statuses:
        return QAStatus.CONDITIONAL
    return QAStatus.ACCEPT
