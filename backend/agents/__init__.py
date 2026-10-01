"""QA agents. Each module exposes ``compute(ctx) -> metrics`` and
``evaluate(metrics, thresholds) -> flags``; the engine runs them in this order,
which is also the order flags appear in reports."""
from backend.agents import alignment, cavity, fluid, geometry, implants, integrity, noise

AGENTS = (geometry, noise, fluid, cavity, implants, alignment, integrity)
