import hashlib
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from host_modules import static_modules
from patch_modes import ArchiveIndex, PatchError, archive_file
from storage import ContentStore
from version_store import VersionStore


PEER='@deepseek-ai/dsh-client-ui-primitives'
SCRIPT='function Z(){return{"'+PEER+'":UI,react:R}}this.modules=r.create({boot:t.__DSH_BOOT__,staticModules:Z(),...options})'


class BrowserPeerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name);self.archive=root/'app.asar';self.exe=root/'Harness.exe';self.exe.write_bytes(b'host')
        self.profile=root/'profile';self.plugin=self.profile/'node_modules/plugin-a';self.plugin.mkdir(parents=True)
        self.meta={'name':'plugin-a','version':'1.0.0','peerDependencies':{PEER:'*'},'dsh':{'client':{'platform':'web'}}}
        self.manifest=self.plugin/'package.json';self.manifest.write_text(json.dumps(self.meta))
        core=SimpleNamespace(resources=root,official_archive=lambda view:self.archive)
        self.versions=VersionStore(self.profile,self.exe,ContentStore(root/'store'),core)
        self.write_archive()

    def write_archive(self, script=SCRIPT, html='<script type="module" src="./assets/index.js"></script>'):
        prefix='dsh/node_modules/@deepseek-ai/dsh-web-frontend/'
        contents={prefix+'package.json':json.dumps({'name':'@deepseek-ai/dsh-web-frontend','version':'0.2.1-rc.3'}).encode(),
                  prefix+'dist/index.html':html.encode(),prefix+'dist/assets/index.js':script.encode()}
        tree={'files':{}};payload=b''
        for name,data in contents.items():
            branch=tree
            for part in name.split('/')[:-1]:branch=branch['files'].setdefault(part,{'files':{}})
            branch['files'][name.split('/')[-1]]={'size':len(data),'offset':str(len(payload)),
                'integrity':{'algorithm':'SHA256','hash':hashlib.sha256(data).hexdigest()}}
            payload+=data
        encoded=json.dumps(tree).encode()
        self.archive.write_bytes(struct.pack('<4I',4,len(encoded)+8,len(encoded)+4,len(encoded))+encoded+payload)

    def test_browser_static_peer_is_pinned_and_drift_still_blocks(self):
        ref=self.versions.package_snapshot('plugin-a')
        receipt=self.versions.store.read_object(ref)['packages']['0']['dependencies'][PEER]
        self.assertEqual(receipt['surface'],'web-static')
        self.assertEqual(receipt['entryHash'],hashlib.sha256(SCRIPT.encode()).hexdigest())
        self.versions.verify_package('plugin-a',ref)
        self.write_archive(SCRIPT+';const changed=1;')
        with self.assertRaisesRegex(PatchError,'依赖内容与版本记录不同'):
            self.versions.verify_package('plugin-a',ref)

    def test_node_only_or_required_dependency_cannot_use_browser_peer(self):
        for meta in [dict(self.meta,dsh={}),dict(self.meta,dependencies={PEER:'*'})]:
            self.manifest.write_text(json.dumps(meta))
            with self.assertRaisesRegex(PatchError,'依赖缺失'):
                self.versions.package_snapshot('plugin-a')

    def test_mention_unregistered_table_dynamic_table_and_unknown_peer_are_rejected(self):
        for script in ['console.log("'+PEER+'")', 'function Z(){return{"'+PEER+'":UI}}',
                       SCRIPT.replace(':UI',':getUI()'), SCRIPT.replace(PEER,'other')]:
            self.write_archive(script)
            with self.assertRaisesRegex(PatchError,'依赖缺失'):
                self.versions.package_snapshot('plugin-a')
        self.assertEqual(static_modules(SCRIPT.replace('react:R','react:R,react:S')),set())

    def test_remote_and_traversing_entrypoints_are_not_followed(self):
        for src in ['https://evil.test/index.js','../assets/index.js','/../assets/index.js']:
            self.write_archive(html='<script type="module" src="'+src+'"></script>')
            with self.assertRaisesRegex(PatchError,'依赖缺失'):
                self.versions.package_snapshot('plugin-a')

    def test_corrupt_official_entry_is_not_exempted(self):
        raw=self.archive.read_bytes();self.archive.write_bytes(raw[:-1]+b'!')
        with self.assertRaisesRegex(PatchError,'依赖缺失'):
            self.versions.package_snapshot('plugin-a')

    def test_shared_index_reuses_only_identical_header_decoding(self):
        index = ArchiveIndex()
        entry = 'dsh/node_modules/@deepseek-ai/dsh-web-frontend/dist/assets/index.js'
        with patch('patch_modes.json.loads', wraps=json.loads) as decode:
            for _ in range(2):
                self.assertEqual(archive_file(self.archive, entry, self.profile, index=index), SCRIPT.encode())
            self.assertEqual(decode.call_count, 1)
        self.write_archive(SCRIPT.replace(':UI', ':UX'))
        self.assertEqual(archive_file(self.archive, entry, self.profile, index=index), SCRIPT.replace(':UI', ':UX').encode())

    def test_shared_index_rechecks_packed_and_unpacked_payloads(self):
        for unpacked in (False, True):
            with self.subTest(unpacked=unpacked):
                data = b'good'
                entry = {'size': 4, 'offset': '0', 'unpacked': unpacked,
                         'integrity': {'algorithm': 'SHA256', 'hash': hashlib.sha256(data).hexdigest()}}
                header = json.dumps({'files': {'item': entry}}).encode()
                self.archive.write_bytes(struct.pack('<4I', 4, len(header)+8, len(header)+4, len(header))+header+data)
                (self.profile / 'item').write_bytes(data)
                index = ArchiveIndex()
                self.assertEqual(archive_file(self.archive, 'item', self.profile, index=index), data)
                if unpacked:
                    (self.profile / 'item').write_bytes(b'evil')
                else:
                    raw = self.archive.read_bytes()
                    self.archive.write_bytes(raw[:-4] + b'evil')
                with self.assertRaisesRegex(PatchError, '完整性校验失败'):
                    archive_file(self.archive, 'item', self.profile, index=index)


if __name__=='__main__':unittest.main()
