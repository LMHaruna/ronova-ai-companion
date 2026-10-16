"""Unicode resource filenames, cache invalidation and safe native paths."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from native_resources import native_resource_path


@unittest.skipUnless(os.name=='nt','Windows native filename compatibility')
class NativeResourceTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory(dir=ROOT/'runtime')
        self.root=Path(self.directory.name)
        self.source=self.root/'桌面'/'model.onnx'
        self.source.parent.mkdir()
        self.source.write_bytes(b'model-v1')
        self.cache=self.root/'cache'

    def tearDown(self):
        self.directory.cleanup()

    def test_ascii_source_needs_no_cache(self):
        source=self.root/'tokens.txt'
        source.write_bytes(b'tokens')
        self.assertEqual(native_resource_path(source),source)
        self.assertFalse(self.cache.exists())

    @patch('native_resources._short_path',return_value=None)
    def test_unicode_source_caches_exact_bytes_and_reuses_the_copy(self,_short):
        target=native_resource_path(self.source,self.cache)
        self.assertTrue(str(target).isascii())
        self.assertEqual(target.read_bytes(),self.source.read_bytes())
        modified=target.stat().st_mtime_ns
        self.assertEqual(native_resource_path(self.source,self.cache),target)
        self.assertEqual(target.stat().st_mtime_ns,modified)
        self.assertEqual(list(self.cache.glob('*.tmp')),[])

    @patch('native_resources._short_path',return_value=None)
    def test_changed_model_and_incomplete_cache_are_not_reused(self,_short):
        first=native_resource_path(self.source,self.cache)
        first.write_bytes(b'partial')
        self.assertEqual(native_resource_path(self.source,self.cache).read_bytes(),b'model-v1')
        self.source.write_bytes(b'model-v2-longer')
        second=native_resource_path(self.source,self.cache)
        self.assertNotEqual(first,second)
        self.assertEqual(second.read_bytes(),self.source.read_bytes())

    @patch('native_resources._short_path',return_value=None)
    def test_unusable_unicode_cache_has_an_actionable_error(self,_short):
        with self.assertRaisesRegex(RuntimeError,'RONOVA_NATIVE_RESOURCE_CACHE'):
            native_resource_path(self.source,self.root/'缓存')


if __name__=='__main__': unittest.main()
