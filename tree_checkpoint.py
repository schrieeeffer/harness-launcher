"""Disposable verified directory indexes, guarded by a continuous NTFS journal.

These indexes never supply rollback bytes or override the expected combination.
Any relevant event invalidates the whole index; unknown evidence is a cache miss.
"""
import hashlib
import json
import logging
import os
from pathlib import Path

from domain import SnapshotEntry, relative_path
from errors import PatchError
from ntfs_journal import JournalUnavailable, NtfsJournal
from storage import ContentStore, atomic_write, canonical


class ProjectedEntries(dict):
    """One fresh, journal-verified view of an immutable tree index."""


class TreeCheckpoints:
    MIN_FILES = 128
    MAX_ENTRIES = 100000
    MAX_TREES = 32

    def __init__(self, path, fingerprint, journal=None):
        self.path = Path(path)
        self.journal = journal or NtfsJournal(fingerprint)
        self.trees = {}
        self.projections = {}
        self.dirty = False
        self.hits = self.misses = 0
        try:
            path = self.safe_path()
            if path.stat().st_size > 64 * 1024 * 1024:
                return
            envelope = json.loads(path.read_bytes())
            trees = envelope['trees']
            if (envelope.get('format') != 2 or not isinstance(trees, dict)
                    or len(trees) > self.MAX_TREES
                    or hashlib.sha256(canonical(trees)).hexdigest() != envelope['checksum']):
                return
            count = 0
            for key, tree in trees.items():
                identity = tree['identity']
                if (not self.numbers(identity, 3) or key != self.key(identity)
                        or not self.numbers(tree['checkpoint'], 2)):
                    return
                entries = tree['entries']
                if not isinstance(entries, dict) or entries.get('.', {}).get('type') != 'dir':
                    return
                count += len(entries)
                if count > self.MAX_ENTRIES:
                    return
                names = set()
                for name, entry in entries.items():
                    relative_path(name, allow_root=True)
                    if name.casefold() in names:
                        return
                    names.add(name.casefold())
                    SnapshotEntry.decode(entry)
                    if not isinstance(entry.get('file_id'), int) or entry['file_id'] <= 0:
                        return
                    if type(entry.get('proof_usn')) is not int or entry['proof_usn'] < 0:
                        return
                    if entry['type'] == 'file' and (not self.numbers(entry.get('stamp'), 3)
                            or not self.numbers(entry.get('ntfsIdentity'), 6)
                            or entry['ntfsIdentity'][0] != identity[0]
                            or entry['ntfsIdentity'][1] != entry['file_id']):
                        return
                    if name != '.' and entries.get(Path(name).parent.as_posix(), {}).get('type') != 'dir':
                        return
                tree['ids'] = {entry['file_id'] for entry in entries.values()}
            self.trees = trees
        except (OSError, ValueError, KeyError, TypeError, PatchError):
            pass

    @staticmethod
    def numbers(value, count):
        return isinstance(value, (list, tuple)) and len(value) == count and all(type(v) is int and v >= 0 for v in value)

    @staticmethod
    def key(identity):
        return ':'.join(map(str, identity))

    def safe_path(self):
        return ContentStore(self.path.parent)._path(self.path.name)

    @staticmethod
    def changed(identity, ids, records):
        root_id = identity[1]
        for file_id, parent_id, reason, usn in records:
            # Renaming the root changes its pathname, not its relative tree.
            # A fresh root identity check is also required at the destination.
            if file_id == root_id and reason and not reason & ~0x80003000:
                continue
            if file_id in ids or parent_id in ids:
                return True
        return False

    def close_entry(self, path, file_id):
        try:
            return self.journal.close_entry(path, file_id)
        except (OSError, JournalUnavailable):
            return None

    def project(self, key, root, tree):
        # Cache only immutable Path construction, never journal evidence. Every
        # caller reaches here after a fresh continuous-log and root-ID check.
        cached = self.projections.get(key)
        if cached is None or cached[0] is not tree['entries']:
            cached = (tree['entries'], {}, {})
            self.projections[key] = cached
        locations = cached[1]
        if root not in locations:
            prefix = str(root) + os.sep
            entries = [(Path(prefix + name), entry) for name, entry in tree['entries'].items()]
            children = {}
            for path, entry in entries:
                children.setdefault(path.parent, set()).add(path)
            locations[root] = entries, children
            while len(locations) > 2:
                locations.pop(next(iter(locations)))
        result = ProjectedEntries()
        result.child_paths = locations[root][1]
        result.fingerprints = cached[2]
        for path, entry in locations[root][0]:
            result[path] = (dict(entry, source=path, stamp=list(entry['stamp']),
                                 ntfsIdentity=list(entry['ntfsIdentity']))
                            if entry['type'] == 'file' else dict(entry))
        return result

    def begin(self, root):
        """Return (reusable entries, candidate checkpoint) for one real root."""
        try:
            identity = self.journal.root_identity(root)
            key = self.key(identity)
            tree = self.trees.get(key)
            if tree:
                try:
                    cursor, records = self.journal.since(root, tree['checkpoint'])
                except JournalUnavailable:
                    cursor, records = None, []
                if (cursor is not None and not self.changed(identity, tree['ids'], records)
                        and self.journal.root_identity(root) == identity):
                    tree['checkpoint'] = cursor
                    self.dirty = True
                    self.hits += 1
                    return self.project(key, root, tree), None
                self.trees.pop(key, None)
                self.projections.pop(key, None)
                self.dirty = True
            checkpoint = self.journal.checkpoint(root)
            self.misses += 1
            return None, (identity, checkpoint)
        except (OSError, JournalUnavailable):
            # Never advance a cursor past an event we could not account for.
            return None, None

    def seal(self, root, candidate, entries):
        """Publish only a fully verified, quiet directory; never a virtual view."""
        identity, checkpoint = candidate
        files = [entry for entry in entries.values() if entry['type'] == 'file']
        if len(files) < self.MIN_FILES or len(entries) > self.MAX_ENTRIES:
            return
        if any('hash' not in e or 'ntfsIdentity' not in e or e['ntfsIdentity'][0] != identity[0] for e in files):
            return  # A writer, unsupported fingerprint, or partial read.
        if any(not entry.get('file_id') or entry.get('proof_usn') is None for entry in entries.values()):
            return
        ids = {entry['file_id'] for entry in entries.values()}
        try:
            cursor, records = self.journal.since(root, checkpoint)
            # Only the exact close records emitted for our verified entries
            # are ours. Never discard unrelated CLOSE or earlier write events.
            barriers = {(entry['file_id'], entry['proof_usn']) for entry in entries.values()}
            records = [r for r in records if not (r[2] & 0x80000000 and (r[0], r[3]) in barriers)]
            if (self.changed(identity, ids, records) or self.journal.root_identity(root) != identity
                    or entries[root]['file_id'] != identity[1]):
                return
        except (OSError, JournalUnavailable):
            return
        saved = {path.relative_to(root).as_posix(): {k: v for k, v in entry.items() if k != 'source'}
                 for path, entry in entries.items()}
        key = self.key(identity)
        self.trees.pop(key, None)
        self.trees[key] = {'identity': identity, 'checkpoint': cursor, 'entries': saved, 'ids': ids}
        self.projections.pop(key, None)
        while len(self.trees) > self.MAX_TREES or sum(len(t['entries']) for t in self.trees.values()) > self.MAX_ENTRIES:
            self.trees.pop(next(iter(self.trees)))
        self.projections = {key: value for key, value in self.projections.items() if key in self.trees}
        self.dirty = True

    def flush(self):
        if not self.dirty:
            return
        try:
            trees = {key: {k: v for k, v in tree.items() if k != 'ids'} for key, tree in self.trees.items()}
            envelope = {'format': 2, 'trees': trees, 'checksum': hashlib.sha256(canonical(trees)).hexdigest()}
            atomic_write(self.safe_path(), canonical(envelope))
            self.dirty = False
        except (OSError, ValueError, PatchError):
            logging.warning('目录检查点未保存；将重新验证。')
