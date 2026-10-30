"""Deletion membership stays indexed instead of growing one process-wide Python set."""
class DeletedIds:
    def __init__(self, owner):
        self.owner = owner

    def __contains__(self, identity):
        return self.owner.conn.execute("SELECT 1 FROM call_deletions WHERE id=?", [identity]).fetchone() is not None

    def __bool__(self):
        return self.owner.conn.execute("SELECT 1 FROM call_deletions LIMIT 1").fetchone() is not None

    def __len__(self):
        return self.owner.conn.execute("SELECT count(*) FROM call_deletions").fetchone()[0]

    def __iter__(self):
        previous = ""
        while True:
            rows = self.owner.conn.execute(
                "SELECT id FROM call_deletions WHERE id>? ORDER BY id LIMIT 250", [previous]).fetchall()
            if not rows:
                return
            previous = rows[-1][0]
            yield from (row[0] for row in rows)

    def update(self, identities):
        pending = []
        for identity in identities:
            pending.append((identity,))
            if len(pending) == 250:
                self.owner.conn.executemany("INSERT OR IGNORE INTO call_deletions VALUES(?)", pending)
                pending.clear()
        if pending:
            self.owner.conn.executemany("INSERT OR IGNORE INTO call_deletions VALUES(?)", pending)

    def clear(self):
        while True:
            result = self.owner.conn.execute(
                "DELETE FROM call_deletions WHERE id IN (SELECT id FROM call_deletions LIMIT 250)")
            if not result.rowcount:
                break
            self.owner.conn.commit()
