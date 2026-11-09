"""IPC paths must match the operator's working decoder command."""
import json
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
import warnings
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from organizer.config import OrganizerConfig,load_organizer_config
from organizer.decoder_runner import select_backend

class DecoderPathsTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.debug=Path(self.tmp.name).resolve()/'disk/debug';self.raw=self.debug/'data/both';self.raw.mkdir(parents=True)
        self.binary=self.debug/'build/decoder';self.binary.parent.mkdir();self.binary.write_text('#!/bin/sh\nexit 0\n');self.binary.chmod(0o755)
        self.manual_config=self.debug/'cfg/config.json';self.manual_config.parent.mkdir();self.manual_config.write_text('{}\n')

    def test_defaults_match_manual_command(self):
        cfg=OrganizerConfig(raw_data_dir=str(self.raw),backend='ipc')
        self.assertEqual(cfg.resolve_decoder_bin(),self.binary)
        self.assertEqual(cfg.resolve_decoder_config(),self.manual_config)
        self.assertEqual(select_backend(cfg),'ipc')

    def test_legacy_missing_path_is_compatible_without_rewriting_settings(self):
        path=Path(self.tmp.name)/'ipc.json';text=json.dumps({'organizer':dict(raw_data_dir=str(self.raw),backend='ipc',decoder_config='../cfg/config.json')});path.write_text(text)
        cfg=load_organizer_config(path)
        with self.assertWarnsRegex(RuntimeWarning,'旧版.*decoder_config'):
            self.assertEqual(cfg.resolve_decoder_config(),self.manual_config)
        with warnings.catch_warnings():
            warnings.simplefilter('ignore',RuntimeWarning);self.assertEqual(select_backend(cfg),'ipc')
        self.assertEqual(path.read_text(),text)

    def test_valid_legacy_and_explicit_paths_are_preserved(self):
        legacy=self.debug/'data/cfg/config.json';legacy.parent.mkdir();legacy.write_text('{"legacy":true}')
        cfg=OrganizerConfig(raw_data_dir=str(self.raw),backend='ipc',decoder_config='../cfg/config.json')
        with warnings.catch_warnings(record=True) as emitted:
            self.assertEqual(cfg.resolve_decoder_config(),legacy)
        self.assertFalse(emitted)
        for name in ('../custom/config.json',str(self.debug/'other/config.json')):
            cfg=OrganizerConfig(raw_data_dir=str(self.raw),backend='ipc',decoder_config=name)
            expected=(self.raw/Path(name)).resolve()
            self.assertEqual(cfg.resolve_decoder_config(),expected)
            with self.assertRaises(FileNotFoundError):select_backend(cfg)

    def test_watch_reports_missing_config_before_opening_state(self):
        from organizer.cli import main
        error=io.StringIO()
        with patch('organizer.cli.StateStore') as state,contextlib.redirect_stderr(error):
            rc=main(['watch','--once','--raw-dir',str(self.raw),'--backend','ipc',
                     '--decoder-config','../custom/config.json'])
        self.assertEqual(rc,1);state.assert_not_called()
        self.assertIn(str((self.raw/'../custom/config.json').resolve()),error.getvalue())

if __name__=='__main__':unittest.main()
