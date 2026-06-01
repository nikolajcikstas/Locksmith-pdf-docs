import json
from pathlib import Path


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def main():
    base_dir = Path(__file__).resolve().parents[1]
    catalog = json.loads((base_dir / "data" / "catalog.json").read_text(encoding="utf-8"))
    lines = ["BEGIN;"]

    for system in catalog["systems"]:
        lines.append(
            "INSERT INTO systems (code, title, system_type, quick_answer, key_remote, mechanical_key, transponder, programming, making_key, decoders, lock_parts, warnings, status) "
            "VALUES ("
            + ", ".join(
                [
                    quote(system["code"]),
                    quote(system["title"]),
                    quote(system["type"]),
                    quote(json.dumps(system["quick_answer"])),
                    quote(json.dumps(system["key_remote"])),
                    quote(json.dumps(system["mechanical_key"])),
                    quote(json.dumps(system["transponder"])),
                    quote(json.dumps(system["programming"])),
                    quote(json.dumps(system["making_key"])),
                    quote(json.dumps(system.get("decoders", []))),
                    quote(json.dumps(system.get("lock_parts", []))),
                    quote(json.dumps(system.get("warnings", []))),
                    quote("approved"),
                ]
            )
            + ") ON CONFLICT (code) DO UPDATE SET "
            "title = EXCLUDED.title, system_type = EXCLUDED.system_type, quick_answer = EXCLUDED.quick_answer, "
            "key_remote = EXCLUDED.key_remote, mechanical_key = EXCLUDED.mechanical_key, transponder = EXCLUDED.transponder, "
            "programming = EXCLUDED.programming, making_key = EXCLUDED.making_key, decoders = EXCLUDED.decoders, "
            "lock_parts = EXCLUDED.lock_parts, warnings = EXCLUDED.warnings, status = EXCLUDED.status;"
        )

    for vehicle in catalog["vehicles"]:
        pages = "ARRAY[" + ",".join(str(page) for page in vehicle.get("source_pages", [])) + "]"
        lines.append(
            "INSERT INTO vehicle_applications (make, model, year_from, year_to, system_code, system_type, source_document, source_pages) "
            "VALUES ("
            + ", ".join(
                [
                    quote(vehicle["make"]),
                    quote(vehicle["model"]),
                    str(vehicle["year_from"]),
                    str(vehicle["year_to"]),
                    quote(vehicle["system_code"]),
                    quote(vehicle.get("type", "")),
                    quote(vehicle.get("source_document", "")),
                    pages,
                ]
            )
            + ");"
        )

    lines.append("COMMIT;")
    out_path = base_dir / "database" / "seed_catalog.sql"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(out_path)


if __name__ == "__main__":
    main()
