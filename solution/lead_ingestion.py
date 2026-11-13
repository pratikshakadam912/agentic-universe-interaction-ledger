
import csv
import json
from pathlib import Path
from collections import Counter

from .normalizers import (
    normalize_header,
    normalize_phone,
    normalize_date,
    normalize_decimal,
    normalize_enum,
)


ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT / "assignment-a-data" / "schemas"


def load_schema(workflow_id):
    schema_path = SCHEMA_DIR / f"{workflow_id}.json"

    with open(schema_path, "r", encoding="utf-8") as file:
        return json.load(file)


def build_header_mapping(headers, fields):
    mapping = {}

    for field in fields:
        aliases = [field["name"]] + field.get("aliases", [])

        normalized_aliases = {
            normalize_header(alias) for alias in aliases
        }

        for header in headers:
            if normalize_header(header) in normalized_aliases:
                mapping[field["name"]] = header
                break

    return mapping


def normalize_field(value, field):
    field_type = field["type"]

    if field_type == "string":
        return value.strip()

    if field_type == "phone":
        return normalize_phone(value)

    if field_type == "date":
        return normalize_date(value)

    if field_type == "decimal":
        return str(normalize_decimal(value))

    if field_type == "enum":
        return normalize_enum(value, field["values"])

    return value.strip()


def process_lead_file(csv_path, workflow_id):
    schema = load_schema(workflow_id)
    fields = schema["fields"]

    batch_id = Path(csv_path).stem

    valid_leads = []
    rejections = []
    warnings = 0
    total_rows = 0

    with open(
        csv_path,
        "r",
        encoding="utf-8-sig",
        newline=""
    ) as file:

        reader = csv.DictReader(file)

        header_mapping = build_header_mapping(
            reader.fieldnames or [],
            fields
        )

        for row in reader:

            # Skip completely blank rows.
            if not any(
                str(value or "").strip()
                for value in row.values()
            ):
                continue

            # Count only non-blank data rows.
            total_rows += 1

            lead = {}
            rejection_reason = None
            row_warnings = 0

            # Process fields in schema order.
            for field in fields:
                field_name = field["name"]
                source_header = header_mapping.get(field_name)

                value = (
                    str(row.get(source_header) or "").strip()
                    if source_header
                    else ""
                )

                # Missing value.
                if not value:
                    lead[field_name] = None

                    if field.get("required"):
                        rejection_reason = (
                            f"missing_required:{field_name}"
                        )
                        break

                    continue

                # Normalize and validate.
                try:
                    lead[field_name] = normalize_field(
                        value,
                        field
                    )

                except ValueError:
                    if field.get("required"):
                        if field["type"] == "phone":
                            rejection_reason = "invalid_phone"
                        else:
                            rejection_reason = (
                                f"invalid_{field['type']}"
                            )

                        break

                    # Invalid optional field.
                    lead[field_name] = None
                    row_warnings += 1

            # Rejected rows must not contribute field warnings.
            if rejection_reason:
                rejections.append({
                    "workflow_id": workflow_id,
                    "batch_id": batch_id,
                    "row_number": total_rows,
                    "reason": rejection_reason
                })
                continue

            # Count warnings only for accepted rows.
            warnings += row_warnings

            lead["workflow_id"] = workflow_id
            lead["batch_id"] = batch_id

            valid_leads.append(lead)

    return {
        "valid_leads": valid_leads,
        "rejections": rejections,
        "total_rows": total_rows,
        "rejected_rows": len(rejections),
        "field_warnings": warnings,
        "rejections_by_reason": dict(
            Counter(
                rejection["reason"]
                for rejection in rejections
            )
        )
    }
