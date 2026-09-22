"""Run a local Qwen formula-extraction smoke test from a Colab terminal."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from payroll.formula.formula_extractor import extract_formula, formula_to_engine_dict

if __name__ == "__main__":
    document = """Lương cơ bản được tính theo số ngày công thực tế / 26. Nhân viên đóng BHXH bằng 8% lương cơ bản."""
    candidate = extract_formula(document, "COLAB_DEMO", [{"source": "manual", "line": 1}])
    print(json.dumps({"confidence": candidate.confidence, "formula": formula_to_engine_dict(candidate.proposed_spec)}, ensure_ascii=False, indent=2, default=str))
