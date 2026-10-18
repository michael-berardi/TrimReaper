"""Synthetic producer registry and host metadata fixtures; no machine cleanup."""
import hashlib
import json
import os
from unittest import mock

from trimreaper import core, host
import test_derived_outputs as fixtures


def load_tests(loader, tests, pattern):
    import unittest
    return unittest.TestSuite(Registry(name) for name in Registry.__dict__ if name.startswith('test_'))


class Registry(fixtures.DerivedOutputs):
    def record(self):
        item = self.item()
        projects = core.reap_output_projects(str(self.repo), str(self.output), item['root'])
        for project in projects:
            lock = core.reap_queue_lock(project)
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.touch()
        record = {'protocol': 'trimreaper-project-locks-v1', 'claimInventory': 'job-meta-paths-v1',
                  'source': str(self.repo), 'output': str(self.output), 'owner': 'J-0700',
                  'kind': 'target', 'lockProjects': projects, 'state': 'finished'}
        for key, path in (('sourceIdentity', self.repo), ('outputIdentity', self.output)):
            info = path.lstat()
            record[key] = {'dev': info.st_dev, 'ino': info.st_ino}
        root = core.reap_queue_state() / 'output-producers'
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = root / (hashlib.sha256(str(self.output).encode()).hexdigest() + '.json')
        path.write_text(json.dumps(record))
        path.chmod(0o600)
        return item, record, path

    def apply_registered(self, item):
        with mock.patch.object(core, 'reap_output_ownership', host.reap_output_ownership), \
             mock.patch.object(core, 'reap_output_producer_guard', self.real_guard):
            return core.reap_item(item)

    @property
    def real_guard(self):
        # The base fixture patches the guard. Restore the actual engine guard body.
        from contextlib import contextmanager
        @contextmanager
        def guard(source, output):
            yield host.reap_output_ownership(source, output)
        return guard

    def test_private_identity_bound_registry_authorizes_exact_output(self):
        item, _, _ = self.record()
        out = self.apply_registered(item)
        self.assertEqual(out['outcome'], 'deleted', out)
        self.assertTrue((self.repo / 'README.md').exists())

    def test_legacy_output_without_registry_stays(self):
        result = self.apply_registered(self.item())
        self.assertIn('output-producer-unverified:registry:', result['reason'])
        self.assertTrue(self.output.exists())

    def test_foreign_association_identity_and_missing_lock_preserve(self):
        for field, value in (('owner', 'J-0701'), ('source', '/not-the-source'),
                             ('outputIdentity', {'dev': 0, 'ino': 0}), ('lockProjects', []),
                             ('protocol', 'unknown')):
            with self.subTest(field=field):
                item, record, path = self.record()
                record[field] = value
                path.write_text(json.dumps(record))
                result = self.apply_registered(item)
                self.assertEqual(result['outcome'], 'refused', result)
                self.assertTrue(self.output.exists())

    def test_symlink_and_public_record_refused(self):
        item, _, path = self.record()
        path.chmod(0o644)
        self.assertIn('unsafe-registry', self.apply_registered(item)['reason'])
        path.unlink()
        path.symlink_to(self.repo / 'README.md')
        self.assertIn('registry:', self.apply_registered(item)['reason'])
        self.assertTrue(self.output.exists())

    def test_active_registered_producer_stays(self):
        item, record, path = self.record()
        record.update(state='active', producer={'pid': 8123, 'start': 'synthetic'})
        path.write_text(json.dumps(record))
        self.table['commands'] = [('8123', 'python producer.py')]
        self.assertIn('producer-still-active', self.apply_registered(item)['reason'])

    def test_detached_source_found_from_host_path_claim_when_cwd_null(self):
        self.detach()
        with core.db() as conn:
            conn.execute("UPDATE jobs SET cwd=NULL,meta=? WHERE id='J-0700'", (json.dumps({'paths': [str(self.repo.parent), str(self.repo)]}),))
        self.assertEqual(self.item()['source'], str(self.repo))

    def test_interrupted_event_must_correlate_with_trusted_finish_clock(self):
        with core.db() as conn:
            stamp = core.now() - 3600
            conn.execute("UPDATE jobs SET status='interrupted',finished_at=?,started_at=?,meta='{}' WHERE id='J-0700'", (stamp, stamp - 100))
            core.log_event(conn, 'job.interrupted', 'J-0700', 'host', {})
            conn.execute("UPDATE events SET ts=? WHERE kind='job.interrupted'", (stamp,))
            job = core.row(conn, "SELECT * FROM jobs WHERE id='J-0700'")
            self.assertIsNone(core.reap_output_terminal_blocker(conn, job, set()))
            job['finished_at'] -= 60
            self.assertEqual(core.reap_output_terminal_blocker(conn, job, set()), 'output-terminal-time-unverified')
