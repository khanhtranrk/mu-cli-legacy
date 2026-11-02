"""Conservative migration creates a candidate, never rewrites a live Spec."""

import json
import re
from copy import deepcopy

from mu.expressions import CEL, LEGACY, engine_for

PATTERN = re.compile(r"\{([A-Za-z_]\w*(?:\[[^\[\]{}]+\])*)\}", re.ASCII)
STRUCTURAL = {
    "name",
    "type",
    "operator",
    "routine",
    "workload",
    "save_state",
    "depends_on",
}


def migrate_spec(spec: dict) -> dict:
    if engine_for(spec) != LEGACY:
        raise ValueError("Spec already uses CEL")
    report = {"spec": {}, "issues": [], "warnings": [], "converted": 0}

    def text(value, path, literal):
        if not re.search(r"[{}]", value):
            return value
        if literal or "${{" in value:
            report["issues"].append(
                f"{path}: inspect structural name or existing CEL manually"
            )
            return value

        def replace(match):
            expression = match[1]
            report["converted"] += 1
            root = re.match(r"[A-Za-z_]\w*", expression, re.ASCII)[0]
            keys = "".join(
                "[" + json.dumps(k, ensure_ascii=False) + "]"
                for k in re.findall(r"\[([^\]]+)\]", expression)
            )
            return "${{ " + root + keys + " }}"

        result = PATTERN.sub(replace, value)
        if re.search(r"[{}]", PATTERN.sub("", value)):
            report["issues"].append(
                f"{path}: unsupported/literal braces; escape or rewrite manually"
            )
        if result != value and not re.fullmatch(r"\{[^{}]+\}", value):
            report["warnings"].append(
                f"{path}: check scalar interpolation (booleans/null use CEL text; containers require whole values)"
            )
        return result

    def visit(value, path, literal=False):
        if isinstance(value, str):
            return text(value, path, literal)
        if isinstance(value, list):
            return [visit(v, f"{path}[{i}]", literal) for i, v in enumerate(value)]
        if isinstance(value, dict):
            result = {}
            for k, v in value.items():
                if re.search(r"[{}]", str(k)):
                    report["issues"].append(
                        f"{path}: dynamic map key requires manual migration"
                    )
                result[k] = visit(v, f"{path}.{k}", k in STRUCTURAL)
            return result
        return deepcopy(value)

    pipelines = spec.get("pipeline", {})
    if not isinstance(pipelines, dict):
        raise TypeError("pipeline must be a mapping before migration")
    for name, pipeline in pipelines.items():
        if (
            isinstance(pipeline, dict)
            and pipeline.get("prepare")
            and pipeline.get("context")
        ):
            report["issues"].append(
                f"pipeline.{name}: move prepare dependencies on pipeline context into shared entry initialization; CEL pipeline context is request-local after prepare"
            )
    report["spec"] = visit(spec, "spec")
    report["spec"]["expression_engine"] = CEL
    report["warnings"].append(
        "Review types, missing-key errors and once-only rendering; run a CPU smoke test before saving/using the candidate. YAML formatting/comments may change."
    )
    return report
