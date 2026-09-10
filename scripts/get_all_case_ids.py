#!/usr/bin/env python3
"""List case IDs from courts_final.<prefix>cases_after_search.

Two modes:

  * default              - dump every id in the table (comma-separated) to a
                           file, for a full export.
  * --stratified         - pick a class-balanced training sample: bucket every
                           case by a normalised case_type into "label-related"
                           (product liability / mass tort / injury torts /
                           malpractice / ...) vs "clean-negative" (contract /
                           foreclosure / tax certiorari / no-fault / ...)
                           strata, then round-robin across the strata in each
                           group until the per-group target is met. Writes a
                           newline-separated id file plus a <name>.strata.tsv
                           sidecar (id, stratum, group, case_type) for review.

The id file is consumed by scripts/export_cases.py --case-ids-file.

Usage:
    # every id -> ny_case_ids.txt
    python scripts/get_all_case_ids.py

    # 5000 label-related + 10000 clean-negative ids, documents required
    python scripts/get_all_case_ids.py --stratified \
        --target-positive 5000 --target-negative 10000 \
        --output data/classification/ny_classification_case_ids.txt

    # also force-in the ids already in the current training set
    python scripts/get_all_case_ids.py --stratified \
        --include-ids-file data/classification/existing_training_ids.txt ...
"""

import argparse
import sys
from collections import OrderedDict, defaultdict
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

sys.path.insert(0, str(Path(__file__).parent.parent))

from lawsuit_parser.utils import load_db_config

# case_type (lowercased, HTML stripped, trimmed) -> stratum. Order matters:
# the first matching WHEN wins, so label-specific torts are tested before the
# generic-tort catch-all and before the negative buckets. "pos_*" strata are
# label-related (they get a real LLM read - some will still come back with no
# label); "neg_*" strata are near-certain all-zero rows.
STRATUM_CASE_SQL = """
CASE
  WHEN ct LIKE '%product liab%' OR ct LIKE '%mass tort%' OR ct LIKE '%zantac%'
    THEN 'pos_product_liability'
  WHEN ct LIKE '%asbestos%' OR ct LIKE '%environmental%'
    THEN 'pos_toxic_tort'
  WHEN ct LIKE '%motor vehicle%'
    THEN 'pos_motor_vehicle'
  WHEN ct LIKE '%malpractice%' OR ct LIKE '%medical, dental%'
    THEN 'pos_malpractice'
  WHEN ct LIKE '%child victims%' OR ct LIKE '%adult survivors%'
    THEN 'pos_abuse'
  WHEN ct LIKE '%no fault%' OR ct LIKE '%no-fault%'
    THEN 'neg_no_fault'
  WHEN ct LIKE '%certiorar%' OR ct LIKE '%assessment review%' OR ct LIKE '%scar%'
    THEN 'neg_tax_certiorari'
  WHEN ct LIKE '%consumer credit%'
    THEN 'neg_consumer_credit'
  WHEN ct LIKE '%foreclosure%' OR ct LIKE '%mortgage%'
    THEN 'neg_foreclosure'
  WHEN ct LIKE '%landlord%' OR ct LIKE '%tenant%'
    THEN 'neg_landlord_tenant'
  WHEN ct LIKE '%workers comp%' OR ct LIKE '%workers'' comp%' OR ct LIKE '%workers compensation%'
    THEN 'neg_workers_comp'
  WHEN ct LIKE '%special proceeding%' OR ct LIKE '%article 75%' OR ct LIKE '%article 78%' OR ct LIKE '%mechanic''s lien%'
    THEN 'neg_special_proceeding'
  WHEN ct LIKE '%commercial%' OR ct LIKE '%contract%' OR ct LIKE '%ucc%' OR ct LIKE '%business entity%'
    THEN 'neg_commercial_contract'
  WHEN ct LIKE '%civil action%' OR ct LIKE '%matrimonial%' OR ct LIKE '%habeas%' OR ct LIKE '%election law%' OR ct LIKE '%forfeiture%' OR ct LIKE '%condemnation%'
    THEN 'neg_other_civil'
  WHEN ct LIKE '%negligence%' OR ct = 'tort' OR ct LIKE 'tort %' OR ct LIKE 'torts -%' OR ct LIKE '%professional malpractice%'
    THEN 'pos_general_tort'
  ELSE 'unmapped'
END
"""


def make_engine(config: dict):
    url = URL.create(
        drivername="postgresql+psycopg",
        username=config["user"],
        password=config["password"],
        host=config["host"],
        port=int(config["port"]),
        database=config["database"],
    )
    return create_engine(url)


def dump_all_ids(engine, cases_table: str, output_file: Path) -> None:
    """Original behaviour: write every id in the table, comma-separated."""
    with engine.connect() as conn:
        total = conn.execute(
            text(f"SELECT COUNT(DISTINCT id) FROM {cases_table}")
        ).scalar()
        print(f"Total cases in {cases_table}: {total:,}")
        case_ids = [
            row[0]
            for row in conn.execute(
                text(f"SELECT DISTINCT id FROM {cases_table} ORDER BY id")
            )
        ]
    print(f"Case IDs range: {min(case_ids)} to {max(case_ids)}")
    output_file.write_text(",".join(map(str, case_ids)))
    print(f"Saved {len(case_ids)} case IDs to {output_file}")


def read_id_file(path: Path) -> list[int]:
    raw = path.read_text().replace("\n", ",")
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


def fetch_stratified_pool(
    engine, cases_table: str, documents_table: str, require_documents: bool, seed: float
) -> list[tuple[int, str, str]]:
    """Return (id, stratum, case_type) for every mapped case, each stratum's
    rows already in a reproducible random order."""
    doc_filter = ""
    if require_documents:
        # ny_docket_documents has no docket_id index, so materialise the
        # distinct set once rather than a per-row EXISTS.
        doc_filter = (
            f"AND s.docket_id IN (SELECT docket_id FROM docd)"
        )
    sql = f"""
        WITH docd AS (
            SELECT DISTINCT docket_id FROM {documents_table}
        ),
        tagged AS (
            SELECT s.id,
                   trim(lower(regexp_replace(coalesce(s.case_type, ''), '<[^>]+>', '', 'g'))) AS ct
            FROM {cases_table} s
            WHERE s.case_id IS NOT NULL
              {doc_filter}
        ),
        strata AS (
            SELECT id, ct, {STRATUM_CASE_SQL} AS stratum FROM tagged
        )
        SELECT id, stratum, ct,
               row_number() OVER (PARTITION BY stratum ORDER BY random()) AS rn
        FROM strata
        WHERE stratum <> 'unmapped'
        ORDER BY stratum, rn
    """
    with engine.connect() as conn:
        conn.execute(text("SET statement_timeout = '900s'"))
        conn.execute(text("SELECT setseed(:s)"), {"s": seed})
        rows = conn.execute(text(sql)).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def round_robin(strata_rows: "dict[str, list]", target: int) -> list:
    """Take items one-at-a-time from each stratum in turn until `target` is
    reached or every stratum is exhausted."""
    chosen: list = []
    cursors = {s: 0 for s in strata_rows}
    while len(chosen) < target:
        progressed = False
        for s, items in strata_rows.items():
            if len(chosen) >= target:
                break
            i = cursors[s]
            if i < len(items):
                chosen.append(items[i])
                cursors[s] += 1
                progressed = True
        if not progressed:
            break
    return chosen


def build_stratified_sample(
    engine,
    cases_table: str,
    documents_table: str,
    output_file: Path,
    target_positive: int,
    target_negative: int,
    include_ids: list[int],
    require_documents: bool,
    seed: float,
) -> None:
    pool = fetch_stratified_pool(engine, cases_table, documents_table, require_documents, seed)
    print(f"Mapped pool: {len(pool):,} cases with a stratum"
          f" ({'documents required' if require_documents else 'documents not required'})")

    include_set = set(include_ids)
    by_stratum: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
    for row in pool:
        if row[0] in include_set:
            continue  # forced in below; don't let round-robin double-count it
        by_stratum[row[1]].append(row)

    print("\nAvailable per stratum:")
    for stratum in sorted(by_stratum):
        print(f"  {stratum:26s} {len(by_stratum[stratum]):6,}")

    pos_strata = OrderedDict(
        (s, by_stratum[s]) for s in sorted(by_stratum) if s.startswith("pos_")
    )
    neg_strata = OrderedDict(
        (s, by_stratum[s]) for s in sorted(by_stratum) if s.startswith("neg_")
    )

    forced = [(cid, "included", "forced") for cid in include_ids]

    pos_pick = round_robin(pos_strata, max(0, target_positive - len(include_set)))
    neg_pick = round_robin(neg_strata, target_negative)

    # De-dup (a forced id may also appear in a stratum) keeping the forced tag.
    seen: set[int] = set()
    final: list[tuple[int, str, str]] = []
    for cid, stratum, ct in forced + pos_pick + neg_pick:
        if cid in seen:
            continue
        seen.add(cid)
        final.append((cid, stratum, ct))

    n_pos = sum(1 for _, s, _ in final if s.startswith("pos_") or s == "included")
    n_neg = sum(1 for _, s, _ in final if s.startswith("neg_"))
    print(f"\nSelected {len(final):,} cases: {n_pos:,} label-related, {n_neg:,} clean-negative")
    if n_pos < target_positive:
        print(f"  WARNING: label-related short of target ({n_pos} < {target_positive}) - pool exhausted")
    if n_neg < target_negative:
        print(f"  WARNING: clean-negative short of target ({n_neg} < {target_negative}) - pool exhausted")

    picked_counts: dict[str, int] = defaultdict(int)
    for _, s, _ in final:
        picked_counts[s] += 1
    print("\nSelected per stratum:")
    for stratum in sorted(picked_counts):
        print(f"  {stratum:26s} {picked_counts[stratum]:6,}")

    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text("\n".join(str(cid) for cid, _, _ in final) + "\n")
    sidecar = output_file.with_suffix(".strata.tsv")
    sidecar.write_text(
        "id\tstratum\tgroup\tcase_type\n"
        + "\n".join(
            f"{cid}\t{s}\t{'positive' if (s.startswith('pos_') or s == 'included') else 'negative'}\t{ct}"
            for cid, s, ct in final
        )
        + "\n"
    )
    print(f"\nSaved {len(final)} case IDs to {output_file}")
    print(f"Stratum breakdown written to {sidecar}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--schema", default="courts_final")
    parser.add_argument("--table-prefix", default="ny_", help="e.g. 'ny_' or 'fl_'")
    parser.add_argument("--port", type=int, default=5433, help="scrapping DB port")
    parser.add_argument("--stratified", action="store_true", help="build a class-balanced sample instead of dumping all ids")
    parser.add_argument("--target-positive", type=int, default=5000)
    parser.add_argument("--target-negative", type=int, default=10000)
    parser.add_argument("--include-ids-file", type=Path, help="ids to force into the positive group (deduped)")
    parser.add_argument("--no-require-documents", dest="require_documents", action="store_false",
                        help="do not restrict to cases that have document rows")
    parser.add_argument("--seed", type=float, default=0.42, help="RNG seed for reproducible sampling")
    parser.add_argument("--output", type=Path, help="output id file (default: ny_case_ids.txt / "
                        "data/classification/<prefix>classification_case_ids.txt for --stratified)")
    args = parser.parse_args()

    config = load_db_config()
    config["port"] = args.port
    engine = make_engine(config)

    cases_table = f"{args.schema}.{args.table_prefix}cases_after_search"
    documents_table = f"{args.schema}.{args.table_prefix}docket_documents"

    try:
        if not args.stratified:
            out = args.output or Path("ny_case_ids.txt")
            dump_all_ids(engine, cases_table, out)
            return

        out = args.output or Path(
            f"data/classification/{args.table_prefix}classification_case_ids.txt"
        )
        include_ids = read_id_file(args.include_ids_file) if args.include_ids_file else []
        if include_ids:
            print(f"Force-including {len(include_ids)} id(s) from {args.include_ids_file}")
        build_stratified_sample(
            engine, cases_table, documents_table, out,
            args.target_positive, args.target_negative,
            include_ids, args.require_documents, args.seed,
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
