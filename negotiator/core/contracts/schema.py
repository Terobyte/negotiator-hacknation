"""JSON Schema export for interoperability with dashboards and external agents."""

import json
from pathlib import Path

from negotiator.core.contracts.models import ApprovedUtterance, BusEvent, CallCard, CallOutcome, JobSpec, JournalEvent, LedgerFact, Quote, Report, TacticEvent

SCHEMAS = (JobSpec, CallCard, ApprovedUtterance, LedgerFact, Quote, TacticEvent, CallOutcome, Report, BusEvent, JournalEvent)


def export_schemas(destination: str | Path) -> None:
    target = Path(destination)
    target.mkdir(parents=True, exist_ok=True)
    for model in SCHEMAS:
        (target / f"{model.__name__}.schema.json").write_text(
            json.dumps(model.model_json_schema(), indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    export_schemas(Path(__file__).with_name("schemas"))
