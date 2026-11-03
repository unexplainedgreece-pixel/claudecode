"""
Migration command line.

    python3 -m config.migrate status
    python3 -m config.migrate upgrade
    python3 -m config.migrate sql                      # show DDL for the active backend
    python3 -m config.migrate claim-orphans you@x.com  # give pre-login posts to a user
"""

import sys

from config.db import BACKEND, DATABASE_URL, DB_PATH, get_connection, init_db
from config.migrations import MIGRATIONS, applied_versions


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "status"
    target = f"PostgreSQL {DATABASE_URL.split('@')[-1]}" if BACKEND == "postgres" else f"SQLite {DB_PATH}"

    if cmd == "status":
        with get_connection() as conn:
            done = applied_versions(conn)
        print(f"Database: {target}")
        for version, name, _ in MIGRATIONS:
            print(f"  {'applied' if version in done else 'PENDING'}  {version:03d} {name}")
        return 0

    if cmd == "upgrade":
        init_db()
        print(f"Database up to date: {target}")
        return 0

    if cmd == "sql":
        # Capture the statements a fresh database would receive, without running them.
        class Recorder:
            backend = BACKEND
            def __init__(self):
                self.statements = []
            def execute(self, sql, params=()):
                self.statements.append(sql.strip())
                class R:
                    rowcount = 0
                    def fetchone(self_inner): return None
                    def fetchall(self_inner): return []
                    def __iter__(self_inner): return iter([])
                return R()
        rec = Recorder()
        import config.migrations as m
        for version, name, fn in MIGRATIONS:
            print(f"-- {version:03d} {name}")
            with_patches = {"table_columns": lambda c, t: set(), "table_exists": lambda c, t: False}
            originals = {k: getattr(m, k) for k in with_patches}
            try:
                for k, v in with_patches.items():
                    setattr(m, k, v)
                fn(rec)
            finally:
                for k, v in originals.items():
                    setattr(m, k, v)
        for s in rec.statements:
            print(s + ";")
        return 0

    if cmd == "claim-orphans":
        if len(argv) < 2:
            print("usage: python3 -m config.migrate claim-orphans EMAIL", file=sys.stderr)
            return 2
        with get_connection() as conn:
            user = conn.execute("SELECT id FROM users WHERE email = ?", (argv[1].lower(),)).fetchone()
            if not user:
                print(f"No user with email {argv[1]}", file=sys.stderr)
                return 1
            n = conn.execute("UPDATE posts SET user_id = ? WHERE user_id IS NULL", (user["id"],)).rowcount
            m = conn.execute("UPDATE pipeline_runs SET user_id = ? WHERE user_id IS NULL", (user["id"],)).rowcount
        print(f"Assigned {n} post(s) and {m} run(s) to {argv[1]}")
        return 0

    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
