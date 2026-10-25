"""Export only built-in fictional cases and independent expected answers."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jolup.samples import list_samples, load_sample
from jolup.settings import DATA

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DATA / "samples")
    destination = parser.parse_args().output
    destination.mkdir(parents=True, exist_ok=True)
    for item in list_samples():
        sample = load_sample(item["id"])
        (destination / f"{item['id']}.csv").write_bytes(sample.pop("csv_bytes"))
        (destination / f"{item['id']}.json").write_text(json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(list_samples())} fictional cases exported. No uploaded data was read.")
