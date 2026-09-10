"""Deployment must fail closed for missing, disabled, or stale harnesses."""
import copy
import hashlib
import importlib.util
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('shipcheck', Path(__file__).parent / 'tools/shipcheck.py')
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)

class ShipcheckTests(unittest.TestCase):
    def setUp(self):
        self.roster = {'ts': time.time(), 'expected': ['box'], 'inactive': set(), 'machines': [
            {'id': 'box', 'online': True, 'kind': 'machine', 'stats': {
                'build': {'code': 'worker', 'ttl': 604800},
                'harnessBuild': hashlib.sha256(b'server').hexdigest()[:12]}}]}

    def verdict(self):
        with patch.object(sc, '_buildinfo', return_value=SimpleNamespace(CADENCE=604800)), \
             patch.object(sc, 'head_code_hash', return_value='worker'), \
             patch.object(sc, 'git', return_value=SimpleNamespace(stdout='server')), \
             patch.object(sc, 'fetch_roster', return_value=copy.deepcopy(self.roster)):
            return sc.fleet_check([])

    def test_matching_processes(self):
        self.assertTrue(self.verdict())

    def test_missing_machine(self):
        self.roster['expected'].append('absent')
        self.assertFalse(self.verdict())

    def test_empty_roster(self):
        self.roster['machines'] = []
        self.assertFalse(self.verdict())

    def test_disabled_old_worker(self):
        self.roster['inactive'] = {'box'}
        self.roster['machines'][0]['stats']['build']['code'] = 'old'
        self.assertFalse(self.verdict())

    def test_old_or_absent_harness(self):
        for value in ('old', None):
            self.roster['machines'][0]['stats']['harnessBuild'] = value
            self.assertFalse(self.verdict())

    def test_offline(self):
        self.roster['machines'][0]['online'] = False
        self.assertFalse(self.verdict())

    def test_missing_inventory(self):
        self.roster.pop('expected')
        self.assertFalse(self.verdict())

if __name__ == '__main__':
    unittest.main()
