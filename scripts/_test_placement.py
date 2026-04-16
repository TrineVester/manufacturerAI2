"""One-off: test placement for session 110000 after outline fix."""
from pathlib import Path
from src.session import load_session
from src.pipeline.design import parse_physical_design, parse_circuit, build_design_spec, validate_design
from src.pipeline.placer import place_components, placement_to_dict
from src.pipeline.config import get_printer
from src.catalog import load_catalog

cat = load_catalog(Path("catalog"))
s = load_session("20260413_110000")

physical = parse_physical_design(s.read_artifact("design.json"))
circuit = parse_circuit(s.read_artifact("circuit.json"))
design = build_design_spec(physical, circuit)

errors = validate_design(design, cat, printer=get_printer(s.printer_id))
if errors:
    print("Validation errors:", errors)
else:
    result = place_components(design, cat)
    data = placement_to_dict(result)
    s.write_artifact("placement.json", data)
    s.clear_step_error("placement")
    s.pipeline_state["placement"] = "complete"
    s.save()
    nc = len(data.get("components", []))
    print(f"Placement OK: {nc} components placed")
