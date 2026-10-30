"""Copy daily call sources without holding the hot writer fence.

Only offset mappings are staged in the existing derived database. Durable
deletion intent remains authoritative until source and projection cutover.
"""
import json
import os

from .runtime import GatewayError
from .storage_admission import StorageAdmission


class CallRewriter:
    def __init__(self, owner):
        self.owner = owner
        self.mapping_rows = 0

    def _copy_batch(self, records, target, operation, admission):
        owner = self.owner
        identities = []
        for _offset, raw in records:
            try:
                value = json.loads(raw)
                identity = value.get("id") if isinstance(value, dict) else None
            except (ValueError, TypeError):
                identity = None
            identities.append(identity)
        with owner.lock:
            removed = {row[0] for row in owner.conn.execute(
                "SELECT id FROM call_deletions WHERE id IN (" +
                ",".join("?" for _ in identities) + ")", identities)}
        mappings = []
        kept = [(record, identity) for record, identity in zip(records, identities, strict=True) if identity not in removed]
        admission.grow(operation, max(4096, target.tell() + sum(len(record[1]) for record, _id in kept)
                                     + (self.mapping_rows + len(kept)) * 128))
        for (offset, raw), identity in zip(records, identities, strict=True):
            if identity in removed:
                continue
            position = target.tell()
            target.write(raw)
            mappings.append((operation, offset, position))
        with owner.lock:
            owner.conn.executemany("INSERT INTO call_rewrite_offsets VALUES(?,?,?)", mappings)
            owner.conn.commit()
        self.mapping_rows += len(mappings)

    def _copy_until(self, source, target, end, operation, admission):
        while source.tell() < end:
            records = []
            for _ in range(250):
                offset = source.tell()
                if offset >= end:
                    break
                raw = source.readline(end - offset)
                if not raw:
                    raise OSError("Call source ended during rewrite")
                records.append((offset, raw))
            self._copy_batch(records, target, operation, admission)

    def rewrite(self, path):
        owner = self.owner
        self.mapping_rows = 0
        with owner.lock, owner.sources.lock:
            if not path.exists():
                return
            name = owner.sources.name(path)
            generation = owner.sources.generation(path)
            stat = path.stat()
            source = path.open("rb")
        admission = None
        try:
            admission = StorageAdmission(owner.root.parent, gate=owner.gate)
            with admission.reserve("calls", 4096) as operation:
                temporary = owner.calls_root / ("." + operation + ".tmp")
                try:
                    with source, temporary.open("xb") as target:
                        self._copy_until(source, target, stat.st_size, operation, admission)
                    # Close the original descriptor before Windows replacement.
                    with owner.lock, owner.sources.lock:
                        current = path.stat()
                        if (owner.sources.generation(path) != generation
                                or current.st_ino != stat.st_ino or current.st_size < stat.st_size):
                            raise GatewayError("log_source_changed", "Call source changed; retry cleanup")
                        with path.open("rb") as tail, temporary.open("ab") as target:
                            tail.seek(stat.st_size)
                            self._copy_until(tail, target, current.st_size, operation, admission)
                            target.flush()
                            os.fsync(target.fileno())
                        new_size = temporary.stat().st_size
                        new_generation = owner.sources.bump(path)
                        try:
                            if new_size:
                                os.replace(temporary, path)
                            else:
                                path.unlink()
                            self._cutover(name, operation, new_size, new_generation)
                        except BaseException:
                            # A failed SQL cutover must never leave readable stale
                            # offsets, even if the physical replace already won.
                            owner._rebuild(paths=[path])
                            raise
                finally:
                    source.close()
                    temporary.unlink(missing_ok=True)
                    with owner.lock:
                        owner.conn.execute("DELETE FROM call_rewrite_offsets WHERE rewrite_id=?", [operation])
                        owner.conn.commit()
        finally:
            source.close()
            if admission is not None:
                admission.close()

    def _cutover(self, name, operation, new_size, generation):
        owner = self.owner
        connection = owner.conn
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""UPDATE calls SET offset=(
                SELECT new_offset FROM call_rewrite_offsets
                WHERE rewrite_id=? AND old_offset=calls.offset)
                WHERE path=? AND EXISTS(SELECT 1 FROM call_rewrite_offsets
                WHERE rewrite_id=? AND old_offset=calls.offset)""", [operation, name, operation])
            # Moving downwards in place can collide with a still-unmoved primary
            # key. A transaction-local negative range makes both passes unique.
            connection.execute("UPDATE call_payload_roots SET offset=-offset-1 WHERE path=?", [name])
            connection.execute("""DELETE FROM call_payload_roots WHERE path=?
                AND NOT EXISTS(SELECT 1 FROM call_rewrite_offsets WHERE rewrite_id=?
                AND old_offset=-call_payload_roots.offset-1)""", [name, operation])
            connection.execute("""UPDATE call_payload_roots SET offset=(
                SELECT new_offset FROM call_rewrite_offsets WHERE rewrite_id=?
                AND old_offset=-call_payload_roots.offset-1) WHERE path=?""", [operation, name])
            connection.execute("""DELETE FROM call_event_sources WHERE path=?
                AND id IN (SELECT id FROM call_deletions)""", [name])
            if new_size:
                owner._set_offset(name, new_size)
                connection.execute("INSERT OR REPLACE INTO call_source_versions VALUES(?,?)", [name, generation])
            else:
                for table in ("index_offsets", "call_source_versions"):
                    connection.execute("DELETE FROM " + table + " WHERE path=?", [name])
            connection.commit()
            owner._invalidate_stats()
        except BaseException:
            connection.rollback()
            raise
