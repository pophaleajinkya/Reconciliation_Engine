"""CLI: `python -m reconcile <case_folder> [<case_folder> ...]`"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from reconcile.pipeline import run_case
from reconcile.schemas import Decision

app = typer.Typer(add_completion=False, help="Reconciliation engine CLI.")
console = Console()


_DECISION_COLOR = {
    Decision.VALID: "red",  # "valid" for retailer == bad for brand
    Decision.INVALID: "green",
    Decision.NEEDS_HUMAN_REVIEW: "yellow",
}


def _render(result: dict) -> None:
    report = result["report"]
    console.print(
        Panel.fit(
            f"[bold]{report.case_name}[/bold]\n"
            f"Invoice: {report.invoice_number or '(unknown)'}\n"
            f"Total deduction claimed: ${report.total_deduction_claimed}\n"
            f"Documents seen: {len(report.documents_seen)} / missing: {report.documents_missing or 'none'}",
            title="Case Summary",
        )
    )

    if report.global_warnings:
        console.print(Panel("\n".join(f"- {w}" for w in report.global_warnings), title="Warnings", style="yellow"))

    table = Table(title="Line Decisions", show_lines=True)
    table.add_column("#", justify="right")
    table.add_column("UPC")
    table.add_column("Description")
    table.add_column("Claim $", justify="right")
    table.add_column("Decision")
    table.add_column("Confidence", justify="right")

    for d in report.line_decisions:
        color = _DECISION_COLOR.get(d.decision, "white")
        table.add_row(
            str(d.claim_index),
            d.upc or "-",
            (d.description or "-")[:48],
            f"{d.claimed_amount:.2f}" if d.claimed_amount is not None else "-",
            f"[{color}]{d.decision.value}[/]",
            f"{d.confidence:.2f} ({d.confidence_band.value})",
        )
    console.print(table)

    for d in report.line_decisions:
        console.rule(f"Line #{d.claim_index} — reasoning")
        for step in d.reasoning:
            console.print(f"• {step.step}")
            for ev in step.evidence:
                console.print(
                    f"    [dim]↳ {ev.doc_type.value} | {ev.field or ''} | {ev.snippet or ''}[/dim]"
                )
        if d.evidence_gaps:
            console.print("[yellow]Evidence gaps:[/yellow]")
            for g in d.evidence_gaps:
                console.print(f"  - {g}")

    console.print(f"\n[green]Artifacts written to:[/green] {result['output_dir']}")


@app.command()
def main(
    cases: list[Path] = typer.Argument(..., exists=True, file_okay=False, dir_okay=True),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging."),
) -> None:
    """Reconcile one or more case folders."""
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    for case_dir in cases:
        try:
            result = run_case(case_dir)
        except Exception as e:
            console.print(f"[red]Failed on {case_dir}: {e}[/red]")
            sys.exit(2)
        _render(result)


if __name__ == "__main__":
    app()
