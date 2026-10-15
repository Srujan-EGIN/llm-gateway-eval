"""Turn a run's results.jsonl into results/REPORT.md and results/results.json.

    uv run python scripts/make_report.py runs/<timestamp>
"""

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATEWAYS = ["litellm", "bifrost", "mlflow", "mlflow_login"]
TITLES = {"litellm": "LiteLLM Proxy", "bifrost": "Bifrost", "mlflow": "MLflow AI Gateway",
          "mlflow_login": "MLflow + login add-on¹"}

# check name -> section. Order here is the order in the report.
SECTIONS = {
    "Basic features": ["test_chat", "test_streaming", "test_tool_calling", "test_embeddings",
                       "test_unknown_model_rejected"],
    "Fallback": ["test_fallback_used", "test_fallback_stays_in_allowed_list"],
    "No-Retention and cost": ["test_no_retention_hardened", "test_retention_default",
                              "test_cost_tracked_default", "test_cost_tracked_with_no_retention"],
    "Security": ["test_requires_key", "test_wrong_key_refused", "test_per_key_model_restriction",
                 "test_management_api_locked",
                 "test_app_credential_cannot_reconfigure", "test_admin_ui_locked", "test_image_pinned"],
    "Speed": ["test_overhead"],
}


def cell(r: dict | None) -> str:
    if r is None:
        return "–"
    d = r["details"]
    if d.get("informational"):
        shown = {k: v for k, v in d.items() if k != "informational"}
        return "ℹ️ " + "; ".join(f"{k}: {v}" for k, v in shown.items())
    icon = {"passed": "✅", "failed": "❌", "skipped": "⏭️"}[r["outcome"]]
    detail = "; ".join(f"{k}: {v}" for k, v in d.items())
    if r["outcome"] == "failed" and r["message"]:
        detail = (detail + "; " if detail else "") + r["message"].split("\n")[0]
    return f"{icon} {detail}".strip()


def esc(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def main(run_dir: Path) -> None:
    rows = [json.loads(line) for line in (run_dir / "results.jsonl").read_text(
        encoding="utf-8").splitlines() if line.strip()]
    targets = json.loads((run_dir / "targets.json").read_text())
    by = {(r["check"], r["gateway"]): r for r in rows}
    docs = {r["check"]: r["doc"] for r in rows}

    score = {}
    for g in GATEWAYS:
        graded = [r for r in rows if r["gateway"] == g and not r["details"].get("informational")]
        score[g] = (sum(r["outcome"] == "passed" for r in graded), len(graded))

    out = [
        "# Results: LiteLLM Proxy vs Bifrost vs MLflow AI Gateway",
        "",
        f"Generated {datetime.now():%Y-%m-%d %H:%M} by `scripts/run_eval.py`. "
        "Every row is an automated test in [`tests/`](../tests).",
        "",
        "| | " + " | ".join(TITLES[g] for g in GATEWAYS) + " |",
        "|---|" + "---|" * len(GATEWAYS),
        "| **Checks passed** | " + " | ".join(f"**{score[g][0]} / {score[g][1]}**"
                                         for g in GATEWAYS) + " |",
        "| Image | " + " | ".join(f"`{targets['images'][g].split('@')[0]}`"
                                for g in GATEWAYS) + " |",
        "",
        "✅ passed  ❌ failed  ℹ️ measured, not graded  – not tested",
        "",
        "¹ MLflow with `--app-name basic-auth` (needs the `-full` image). Tested for **security "
        "checks only**; every other behaviour is the same as plain MLflow.",
        "",
    ]
    for section, checks in SECTIONS.items():
        present = [c for c in checks if any((c, g) in by for g in GATEWAYS)]
        if not present:
            continue
        out += [f"## {section}", "", "| Check | " + " | ".join(TITLES[g] for g in GATEWAYS) + " |",
                "|---|" + "---|" * len(GATEWAYS)]
        for c in present:
            out.append(f"| {esc(docs.get(c) or c)} | "
                       + " | ".join(esc(cell(by.get((c, g)))) for g in GATEWAYS) + " |")
        out.append("")

    out += [
        "## How to read this",
        "",
        "- **Hardened** means each gateway's recommended production settings: LiteLLM with "
        "`turn_off_message_logging`, Bifrost with `disable_content_logging`, MLflow endpoints with "
        "usage tracking (request traces) off. "
        "**Default** means the out-of-the-box settings.",
        "- The provider is a **fake model** (`mock_provider/`) that answers in ~100 ms, so tests are "
        "free and repeatable. Real Bedrock was not called in this run.",
        "- Speed numbers come from one machine with everything in Docker. Compare the two gateways "
        "with each other, not with other benchmarks.",
        "",
    ]
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results" / "REPORT.md").write_text("\n".join(out), encoding="utf-8")
    (ROOT / "results" / "results.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print_verdict(rows, score)


def print_verdict(rows: list[dict], score: dict) -> None:
    """Short per-gateway summary at the end of the console output."""
    bar = "=" * 72
    print(f"\n{bar}\n  VERDICT PER GATEWAY  (full details: results/REPORT.md)\n{bar}")
    for g in GATEWAYS:
        passed, total = score[g]
        if not total:
            continue
        failed = [r["doc"] or r["check"] for r in rows
                  if r["gateway"] == g and r["outcome"] == "failed"]
        status = "ALL CHECKS PASSED" if not failed else f"{len(failed)} CHECK(S) FAILED"
        scope = " (security checks only)" if g == "mlflow_login" else ""
        print(f"\n  {TITLES[g].rstrip('¹')}{scope}: {passed}/{total}  ->  {status}")
        for f in failed:
            print(f"      x {f}")
    print(f"\n{bar}\n  A failed check is a finding about that gateway, not a broken test.\n{bar}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
