"""SQLite persistence: healer runs, recipes, audit — in the unified state.db.

Connections are short-lived (opened and closed per operation via
``closing(self._connect())``) — handlers and hooks may hit the store from
independent call sites and WAL keeps readers/writers happy.

Unified-plugin adaptation (plan D4/Appendix A): the store is an adapter over
the consolidated ``state.db`` — the ``runs`` table is named ``healer_runs``
there, and schema/migrations are owned by ``storage.state`` (whose v1 DDL
already includes the legacy v2 ``relaxed_key`` column). The legacy v1→v2
backfill knowledge lives in the legacy-data importer, not here. All query
logic below is verbatim from hermes-flaky-healer.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .. import redact as redact_mod
from .signature import is_degenerate, relaxed_key

logger = logging.getLogger(__name__)

# Informational: the legacy store's last schema version (the unified state.db
# supersedes it; kept because callers/tests reference the constant).
SCHEMA_VERSION = 2


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Recipe:
    signature: str
    created_ts: str
    diagnosis: dict
    strategy: str
    patch_ops: list[dict]
    signature_parts: dict
    hits: int = 0
    successes: int = 0
    failures: int = 0
    last_used_ts: str | None = None

    def to_dict(self) -> dict:
        return {
            "signature": self.signature,
            "sig8": self.signature[:8],
            "created_ts": self.created_ts,
            "diagnosis": self.diagnosis,
            "strategy": self.strategy,
            "patch_ops": self.patch_ops,
            "signature_parts": self.signature_parts,
            "hits": self.hits,
            "successes": self.successes,
            "failures": self.failures,
            "last_used_ts": self.last_used_ts,
        }


class HealerStore:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Fail fast + create the schema once at construction (the unified
        # connection factory applies it idempotently on every open anyway).
        with closing(self._connect()):
            pass

    def _connect(self) -> sqlite3.Connection:
        from ....storage import state

        return state.connect(self.db_path)

    # -- runs -----------------------------------------------------------------

    def record_run(
        self,
        *,
        source: str,
        build_id: str | None,
        test_id: str,
        diagnosis: dict | None,
        strategy: str | None,
        result: str,
        isolation: str | None,
        duration_s: float,
    ) -> int:
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                "INSERT INTO healer_runs(ts, source, build_id, test_id, diagnosis_json, strategy,"
                " result, isolation, duration_s) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    _now(),
                    source,
                    build_id,
                    test_id,
                    json.dumps(diagnosis or {}),
                    strategy,
                    result,
                    isolation,
                    round(duration_s, 3),
                ),
            )
            return int(cur.lastrowid)

    def runs(self, limit: int = 50) -> list[dict]:
        with closing(self._connect()) as conn, conn:
            rows = conn.execute(
                "SELECT * FROM healer_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for row in rows:
            entry = dict(row)
            entry["diagnosis"] = json.loads(entry.pop("diagnosis_json") or "{}")
            out.append(entry)
        return out

    # -- audit ------------------------------------------------------------------

    def audit(self, event: str, payload) -> None:
        try:
            encoded = json.dumps(payload)
        except TypeError:
            # Last line of defence: a non-serializable payload must never
            # smuggle a raw credential into the audit table via its repr().
            encoded = json.dumps(redact_mod.repr_fallback(payload))
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO audit(ts, event, payload_json) VALUES(?,?,?)",
                (_now(), event, encoded),
            )

    def audit_rows(self, limit: int = 50) -> list[dict]:
        with closing(self._connect()) as conn, conn:
            rows = conn.execute(
                "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for row in rows:
            entry = dict(row)
            entry["payload"] = json.loads(entry.pop("payload_json") or "{}")
            out.append(entry)
        return out

    # -- recipes ----------------------------------------------------------------

    # ON CONFLICT: the recipe BODY (strategy + patch_ops) is refreshed; the
    # hits/successes/failures counters and last_used_ts describe how the STORED
    # body performed, so they only survive when the body is unchanged — a
    # replaced body starts from zero rather than inheriting rank earned (or
    # failures suffered) by the previous patch. `IS NOT` is NULL-safe.
    _RECIPE_BODY_CHANGED = (
        "(excluded.patch_ops_json IS NOT recipes.patch_ops_json"
        " OR excluded.strategy IS NOT recipes.strategy)"
    )

    def upsert_recipe(
        self,
        signature: str,
        *,
        diagnosis: dict,
        strategy: str,
        patch_ops: list[dict],
        signature_parts: dict,
    ) -> None:
        if signature_parts and is_degenerate(signature_parts):
            # An all-empty signature (no error, no failing action — e.g. the
            # trace of a passing retry) hashes to a constant under which
            # unrelated failures would collide; never persist a recipe for it.
            logger.debug(
                "skipping recipe upsert for %s: degenerate signature parts", signature[:8]
            )
            return
        blob = json.dumps({"diagnosis": diagnosis, "signature_parts": signature_parts})
        ops = json.dumps(patch_ops)
        rkey = relaxed_key(signature_parts) if signature_parts else None
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO recipes(signature, created_ts, diagnosis_json, strategy,"
                " patch_ops_json, relaxed_key) VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(signature) DO UPDATE SET diagnosis_json=excluded.diagnosis_json,"
                " strategy=excluded.strategy, patch_ops_json=excluded.patch_ops_json,"
                " relaxed_key=excluded.relaxed_key,"
                f" hits=CASE WHEN {self._RECIPE_BODY_CHANGED} THEN 0 ELSE recipes.hits END,"
                f" successes=CASE WHEN {self._RECIPE_BODY_CHANGED} THEN 0"
                "  ELSE recipes.successes END,"
                f" failures=CASE WHEN {self._RECIPE_BODY_CHANGED} THEN 0"
                "  ELSE recipes.failures END,"
                f" last_used_ts=CASE WHEN {self._RECIPE_BODY_CHANGED} THEN NULL"
                "  ELSE recipes.last_used_ts END",
                (signature, _now(), blob, strategy, ops, rkey),
            )

    @staticmethod
    def _row_to_recipe(row: sqlite3.Row) -> Recipe:
        blob = json.loads(row["diagnosis_json"] or "{}")
        if "diagnosis" in blob:
            diagnosis, parts = blob.get("diagnosis") or {}, blob.get("signature_parts") or {}
        else:  # tolerate plain diagnosis payloads
            diagnosis, parts = blob, {}
        return Recipe(
            signature=row["signature"],
            created_ts=row["created_ts"],
            diagnosis=diagnosis,
            strategy=row["strategy"],
            patch_ops=json.loads(row["patch_ops_json"] or "[]"),
            signature_parts=parts,
            hits=row["hits"],
            successes=row["successes"],
            failures=row["failures"],
            last_used_ts=row["last_used_ts"],
        )

    def get_recipe(self, signature: str) -> Recipe | None:
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT * FROM recipes WHERE signature = ?", (signature,)
            ).fetchone()
        return self._row_to_recipe(row) if row else None

    def all_recipes(self) -> list[Recipe]:
        with closing(self._connect()) as conn, conn:
            rows = conn.execute("SELECT * FROM recipes ORDER BY created_ts").fetchall()
        return [self._row_to_recipe(r) for r in rows]

    def recipes_by_relaxed_key(self, key: str) -> list[Recipe]:
        """Recipes sharing a relaxed signature key — an indexed lookup that
        replaces scanning every recipe and rehashing it (perf §2.1). NULL keys
        (recipes stored without signature_parts) never match."""
        with closing(self._connect()) as conn, conn:
            rows = conn.execute(
                "SELECT * FROM recipes WHERE relaxed_key = ? ORDER BY created_ts", (key,)
            ).fetchall()
        return [self._row_to_recipe(r) for r in rows]

    def bump_recipe(self, signature: str, *, success: bool) -> None:
        column = "successes" if success else "failures"
        with closing(self._connect()) as conn, conn:
            conn.execute(
                f"UPDATE recipes SET hits = hits + 1, {column} = {column} + 1,"
                " last_used_ts = ? WHERE signature = ?",
                (_now(), signature),
            )
