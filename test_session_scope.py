import copy
from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from session_scope import SessionScope, SessionScopeError


def _claim_process(path, name, thread_id, barrier):
    scope = SessionScope({'session_scope_file': path, 'session_scope': name})
    barrier.wait(timeout=10)
    scope.claim_created(thread_id)


class SessionScopeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'scope.json'
        self.registry = {'version': 1, 'default_scope': 'general',
                         'scopes': ['general', 'research', 'development'],
                         'assignments': {'paper': 'research', 'code': 'development'}}
        self.write(self.registry)

    def write(self, registry):
        self.path.write_text(json.dumps(registry), encoding='utf-8')

    def scope(self, name='research'):
        return SessionScope({'session_scope_file': str(self.path), 'session_scope': name})

    def replace(self, registry):
        replacement = self.path.with_suffix('.replacement')
        replacement.write_text(json.dumps(registry), encoding='utf-8')
        # Same timestamps and size must not conceal an atomic inode replacement.
        previous = self.path.stat()
        os.utime(replacement, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        os.replace(replacement, self.path)

    def test_unconfigured_instances_preserve_legacy_behavior(self):
        scope = SessionScope({})
        threads = [{'id': 'paper'}, {'id': 'code'}, {'unexpected': True}]
        self.assertFalse(scope.enabled)
        self.assertTrue(scope.allows('paper'))
        scope.require('code')
        scope.claim_created('new')
        self.assertEqual(scope.filter_threads(threads), threads)
        self.assertEqual(json.loads(self.path.read_text()), self.registry)

    def test_incomplete_or_invalid_configuration_fails_closed(self):
        for config in ({'session_scope': 'research'}, {'session_scope_file': str(self.path)},
                       {'session_scope_file': 'relative.json', 'session_scope': 'general'},
                       {'session_scope_file': str(self.path), 'session_scope': 'unknown'},
                       {'session_scope_file': str(self.path), 'session_scope': []},
                       {'session_scope_file': str(self.path), 'session_scope': {}},
                       {'session_scope_file': None, 'session_scope': 'general'}):
            with self.subTest(config=config), self.assertRaises(SessionScopeError):
                SessionScope(config)

    def test_assignments_and_default_scope_are_applied(self):
        research, general, development = self.scope(), self.scope('general'), self.scope('development')
        self.assertTrue(research.enabled)
        self.assertTrue(research.allows('paper'))
        self.assertFalse(research.allows('code'))
        self.assertFalse(research.allows('unassigned'))
        self.assertTrue(general.allows('unassigned'))
        self.assertFalse(general.allows('paper'))
        self.assertTrue(development.allows('code'))
        research.require('paper')
        with self.assertRaises(SessionScopeError):
            research.require('code')

    def test_filter_preserves_order_and_objects_and_rejects_invalid_ids(self):
        paper, another = {'id': 'paper', 'name': 'Paper'}, {'id': 'another'}
        result = self.scope().filter_threads([another, {'id': 'code'}, paper, {}, None, {'id': 123}])
        self.assertEqual(result, [paper])
        self.assertIs(result[0], paper)
        for invalid in (None, 123, '', ' paper', 'paper\n'):
            self.assertFalse(self.scope().allows(invalid))
            with self.assertRaises(SessionScopeError):
                self.scope().require(invalid)

    def test_registry_fields_are_all_validated(self):
        invalid = [None, [], {}, {**self.registry, 'version': True}, {**self.registry, 'version': 2},
                   {**self.registry, 'default_scope': 'missing'},
                   {**self.registry, 'scopes': 'research'},
                   {**self.registry, 'scopes': ['general', 'research', 'research']},
                   {**self.registry, 'scopes': ['general', 'research']},
                   {**self.registry, 'assignments': []},
                   {**self.registry, 'assignments': {'': 'research'}},
                   {**self.registry, 'assignments': {'bad\n': 'research'}},
                   {**self.registry, 'assignments': {'paper': 'other'}},
                   {**self.registry, 'assignments': {'paper': None}}]
        for field in self.registry:
            missing = copy.deepcopy(self.registry)
            del missing[field]
            invalid.append(missing)
        for registry in invalid:
            with self.subTest(registry=registry):
                self.write(registry)
                with self.assertRaises(SessionScopeError):
                    self.scope()

    def test_missing_invalid_duplicate_or_unreadable_registry_is_rejected(self):
        self.path.unlink()
        with self.assertRaises(SessionScopeError):
            self.scope()
        self.path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(SessionScopeError):
            self.scope()
        self.path.write_text('{"version":1,"version":1}', encoding='utf-8')
        with self.assertRaises(SessionScopeError):
            self.scope()
        self.write(self.registry)
        with patch('session_scope.os.open', side_effect=PermissionError('private path')):
            with self.assertRaises(SessionScopeError) as error:
                self.scope()
        self.assertNotIn('private path', str(error.exception))

    def test_symlink_registry_is_rejected(self):
        original = self.path.with_suffix('.original')
        self.path.rename(original)
        self.path.symlink_to(original)
        with self.assertRaises(SessionScopeError):
            self.scope()

    def test_cached_registry_observes_atomic_replacement(self):
        scope = self.scope()
        self.assertTrue(scope.allows('paper'))
        changed = copy.deepcopy(self.registry)
        changed['assignments']['paper'] = 'general'
        self.replace(changed)
        self.assertFalse(scope.allows('paper'))
        self.assertEqual(scope.filter_threads([{'id': 'paper'}]), [])

    def test_cache_never_hides_deleted_or_corrupt_registry(self):
        scope = self.scope()
        self.path.unlink()
        with self.assertRaises(SessionScopeError):
            scope.allows('paper')
        with self.assertRaises(SessionScopeError):
            scope.claim_created('new')
        self.assertFalse(self.path.exists())
        self.path.write_text('bad registry', encoding='utf-8')
        with self.assertRaises(SessionScopeError):
            scope.filter_threads([{'id': 'paper'}])

    def test_claim_records_only_new_id_and_saves_private_file(self):
        scope = self.scope()
        scope.claim_created('new-native')
        registry = json.loads(self.path.read_text())
        self.assertEqual(registry['assignments'], {**self.registry['assignments'], 'new-native': 'research'})
        self.assertTrue(scope.allows('new-native'))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.with_name('scope.json.lock').stat().st_mode), 0o600)
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_claim_same_scope_is_idempotent_but_foreign_claim_cannot_overwrite(self):
        scope = self.scope()
        original, inode = self.path.read_bytes(), self.path.stat().st_ino
        scope.claim_created('paper')
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.path.stat().st_ino, inode)
        with self.assertRaises(SessionScopeError):
            scope.claim_created('code')
        self.assertEqual(self.path.read_bytes(), original)

    def test_claim_explicitly_records_default_scope_for_new_session(self):
        self.scope('general').claim_created('new-general')
        self.assertEqual(json.loads(self.path.read_text())['assignments']['new-general'], 'general')

    def test_claim_reloads_registry_under_lock_instead_of_overwriting_cached_data(self):
        scope = self.scope()
        changed = copy.deepcopy(self.registry)
        changed['assignments']['other-created'] = 'development'
        self.replace(changed)
        scope.claim_created('new-research')
        self.assertEqual(json.loads(self.path.read_text())['assignments'],
                         {**changed['assignments'], 'new-research': 'research'})

    def test_concurrent_distinct_claims_do_not_lose_assignments(self):
        count, barrier = 8, threading.Barrier(8)

        def claim(index):
            name = ['general', 'research', 'development'][index % 3]
            scope = self.scope(name)  # Every worker holds an old cached snapshot.
            barrier.wait(timeout=10)
            scope.claim_created('parallel-' + str(index))
            return 'parallel-' + str(index), name

        with ThreadPoolExecutor(max_workers=count) as executor:
            added = dict(executor.map(claim, range(count)))
        self.assertEqual(json.loads(self.path.read_text())['assignments'],
                         {**self.registry['assignments'], **added})

    def test_competing_claims_cannot_steal_the_same_thread(self):
        barrier = threading.Barrier(2)

        def claim(name):
            scope = self.scope(name)
            barrier.wait(timeout=10)
            try:
                scope.claim_created('contested')
                return name, True
            except SessionScopeError:
                return name, False

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, ['research', 'development']))
        winners = [name for name, succeeded in results if succeeded]
        self.assertEqual(len(winners), 1)
        self.assertEqual(json.loads(self.path.read_text())['assignments']['contested'], winners[0])

    def test_independent_processes_share_one_stable_registry_lock(self):
        context = multiprocessing.get_context('fork')
        barrier = context.Barrier(4)
        names = ['research', 'development', 'general', 'research']
        processes = [context.Process(target=_claim_process,
                     args=(str(self.path), name, 'process-' + str(index), barrier))
                     for index, name in enumerate(names)]
        try:
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=15)
                self.assertEqual(process.exitcode, 0)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join()
        self.assertEqual(json.loads(self.path.read_text())['assignments'],
                         {**self.registry['assignments'],
                          **{'process-' + str(index): name for index, name in enumerate(names)}})

    def test_symlink_or_fifo_lock_cannot_modify_another_file_or_block_a_claim(self):
        scope = self.scope()
        target = self.path.with_suffix('.target')
        target.write_text('untouched', encoding='utf-8')
        lock = self.path.with_name('scope.json.lock')
        lock.symlink_to(target)
        with self.assertRaises(SessionScopeError):
            scope.claim_created('new')
        self.assertEqual(target.read_text(), 'untouched')
        lock.unlink()
        os.mkfifo(lock, 0o600)
        with self.assertRaises(SessionScopeError):
            scope.claim_created('new')
        self.assertNotIn('new', json.loads(self.path.read_text())['assignments'])

    def test_failed_replace_keeps_original_registry_and_cleans_temporary_file(self):
        scope, original = self.scope(), self.path.read_bytes()
        with patch('session_scope.os.replace', side_effect=OSError('disk unavailable')):
            with self.assertRaises(SessionScopeError):
                scope.claim_created('new')
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
