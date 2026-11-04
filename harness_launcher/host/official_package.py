"""Shared authenticated installer inventory. No network, no executable cache copies.

Receipts are bound to SHA-256 content (read through the strong NTFS cache), the
requested SHA-512 and a time-limited Authenticode result. Extraction is performed
once per package, not once per consumer. Temporary payloads are never trusted
without checking their recorded hashes and are released by the owning operation.
"""
from contextlib import contextmanager
import base64
import hashlib
import json
import logging
import shutil
import time
import uuid

from harness_launcher.domain.models import relative_path
from harness_launcher.app.operations import report_progress
from harness_launcher.storage.content_store import ContentStore, hash_file, write_json
from harness_launcher.host import runtime_repair as repair


class OfficialPackages:
    def __init__(self, manager):
        self.manager = manager
        self.store = ContentStore(manager.data / 'official-packages')
        self.payloads = {}

    @contextmanager
    def session(self):
        # Nested consumers share the same extraction within one locked operation.
        before = set(self.payloads)
        try:
            yield self
        finally:
            for key in set(self.payloads) - before:
                stage, _ = self.payloads.pop(key)
                try:
                    shutil.rmtree(repair.safe_path(stage))
                except OSError:
                    logging.warning('官方包临时解压目录暂未清理：%s', stage)

    def read(self, ref):
        value = self.store.read_object(ref)
        if value.get('kind') != 'official-package' or value.get('format') != 1:
            repair.fail('官方安装包缓存格式无效。')
        for name, entry in value['files'].items():
            relative_path(name)
            if not isinstance(entry['size'], int) or entry['size'] < 0 or len(entry['sha256']) != 64:
                repair.fail('官方文件清单无效。')
        return value

    def inspect(self, installer=None, sha512=None):
        source, expected = self.manager.runtime_repair.candidate(installer, sha512)
        expected = base64.b64encode(expected).decode()
        with repair.read_lease(source):
            digest = self.manager.verification_cache.hash(source, hash_file)
            index = self.store._path('receipts/' + digest + '.json')
            cached = None
            receipt = {}
            if index.exists():
                try:
                    receipt = json.loads(index.read_bytes())
                    cached = self.read(receipt['manifest'])
                    if cached['sha256'] != digest or cached['sha512'] != expected:
                        cached = None
                except (OSError, ValueError, KeyError, TypeError, repair.LauncherError):
                    cached = None
                    receipt = {}
            if cached is None:
                report_progress('packages', '验证官方包 SHA-512 与发布者签名')
                with source.open('rb') as stream:
                    actual = base64.b64encode(hashlib.file_digest(stream, 'sha512').digest()).decode()
                if actual != expected:
                    repair.fail('安装包 SHA-512 不匹配。', 'runtime_checksum_mismatch')
            # Revocation and trust-store changes are not cached indefinitely.
            now = time.time()
            verified_at = receipt.get('verifiedAt', 0)
            if not isinstance(verified_at, (int, float)):
                verified_at = 0
            if cached is None or not 0 <= now - verified_at < 86400:
                signed = repair.signature(source)
                verified_at = now
            else:
                signed = cached['signed']
            if cached is None:
                stage = self.store._path('staging/' + uuid.uuid4().hex)
                stage.mkdir(parents=True)
                try:
                    report_progress('packages', '建立共享官方文件清单（此包只解包一次）')
                    payload, sizes = repair.SevenZip().extract_all(source, stage)
                    files = {}
                    for i, (name, size) in enumerate(sizes.items(), 1):
                        path = repair.safe_path(payload / name)
                        if not repair.regular(path) or path.stat().st_size != size:
                            repair.fail('解压文件与官方清单不一致：' + name)
                        files[name] = {'size': size, 'sha256': hash_file(path)}
                        if i % 50 == 0 or i == len(sizes):
                            report_progress('files', '记录官方文件内容', i, len(sizes))
                    cached = {'format': 1, 'kind': 'official-package', 'sha256': digest,
                              'sha512': expected, 'signed': signed, 'files': files}
                    self.payloads[digest] = (stage, payload)
                except BaseException:
                    shutil.rmtree(repair.safe_path(stage))
                    raise
            cached = dict(cached, signed=signed)
            ref = self.store.object('baselines', cached)
            updated = {'manifest': ref, 'verifiedAt': verified_at}
            if receipt != updated:
                write_json(index, updated)
            return {'reference': ref, 'source': str(source), 'value': cached}

    def materialize(self, package, names, stage):
        """Extract only missing files on a cache hit; never download another copy."""
        value = package['value']
        if any(name not in value['files'] for name in names):
            repair.fail('请求文件不属于已认证的官方包。')
        cached = self.payloads.get(value['sha256'])
        if cached:
            payload = cached[1]
        else:
            with repair.read_lease(package['source']):
                fresh = self.inspect(package['source'], value['sha512'])
                if fresh['value']['files'] != value['files']:
                    repair.fail('官方包内容发生变化。')
                payload = repair.SevenZip().extract_members(package['source'], stage, names)
        for name in names:
            if hash_file(repair.safe_path(payload / name)) != value['files'][name]['sha256']:
                repair.fail('临时修复文件校验失败：' + name)
        return payload
