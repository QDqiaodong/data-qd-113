import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from terrain_flow import lake
from terrain_flow.drainage import drain

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures/terrain.json'


def write_json(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')
    return path


class LakeTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.lake = self.tmp / 'lake'
        # Manifest paths resolve relative to the manifest, which lives here.
        self.sources = self.tmp

    def tearDown(self):
        self._tmp.cleanup()

    def source(self, name, value=None, raw=None):
        path = self.sources / name
        if raw is not None:
            path.write_bytes(raw)
        else:
            write_json(path, value
                       if value is not None
                       else json.loads(FIXTURE.read_text()))
        return path

    def manifest(self, items, name='manifest.json'):
        path = self.tmp / name
        write_json(path, {'sources': items})
        return path

    def ingest(self, items, name='manifest.json'):
        return lake.ingest(self.manifest(items, name), self.lake)

    def partition(self, source_id, version=1):
        return self.lake / 'sources' / source_id / ('v%d' % version)

    def report(self, source_id, version=1):
        return json.loads((self.partition(source_id, version) / 'report.json').read_text())

    def catalog(self):
        return json.loads((self.lake / 'catalog.json').read_text())


class BatchCommitTests(LakeTestCase):
    def test_commits_one_partition_per_source_with_exact_single_file_report(self):
        a = self.source('a.json')
        b = self.source('b.json', {'cell_size_m': 1, 'elevations': [[2, 1], [None, 0]]})
        summary = self.ingest([{'id': 'alpha', 'path': a.name},
                               {'id': 'beta', 'path': b.name, 'stream_threshold': 1}])
        self.assertEqual(summary['counts'],
                         {'total': 2, 'committed': 2, 'skipped': 0, 'failed': 0})
        self.assertEqual(self.report('alpha'), drain(json.loads(a.read_text())))
        self.assertEqual(self.report('beta'),
                         drain(json.loads(b.read_text()), 1))
        self.assertEqual(sorted(os.listdir(self.partition('alpha'))),
                         ['meta.json', 'report.json'])
        meta = json.loads((self.partition('alpha') / 'meta.json').read_text())
        self.assertEqual(meta['input_sha256'], lake._digest(a.read_bytes()))
        self.assertEqual(meta['params'], {'stream_threshold': None})
        self.assertEqual(meta['artifact']['sha256'],
                         lake._digest((self.partition('alpha') / 'report.json').read_bytes()))

    def test_catalog_lists_every_partition_sorted_and_verifiable(self):
        for source_id in ('zeta', 'alpha', 'mid'):
            self.source(source_id + '.json')
        summary = self.ingest([{'id': s, 'path': s + '.json'}
                               for s in ('zeta', 'alpha', 'mid')])
        self.assertEqual([r['source_id'] for r in summary['sources']],
                         ['alpha', 'mid', 'zeta'])
        catalog = self.catalog()
        self.assertEqual([e['partition'] for e in catalog['partitions']],
                         ['sources/alpha/v1', 'sources/mid/v1', 'sources/zeta/v1'])
        for entry in catalog['partitions']:
            self.assertEqual(entry['status'], 'committed')
            self.assertEqual(entry['artifact_sha256'],
                             lake._digest((self.lake / entry['partition'] /
                                           'report.json').read_bytes()))
        self.assertEqual(lake.verify_lake(self.lake),
                         {'ok': True, 'partitions': 3,
                          'catalog_matches': True, 'problems': []})

    def test_reports_are_independently_parseable_and_not_concatenated(self):
        self.source('a.json')
        self.source('b.json', {'cell_size_m': 3, 'elevations': [[5]]})
        self.ingest([{'id': 'a', 'path': 'a.json'},
                     {'id': 'b', 'path': 'b.json', 'stream_threshold': 2}])
        for source_id in ('a', 'b'):
            raw = (self.partition(source_id) / 'report.json').read_bytes()
            self.assertIsInstance(json.loads(raw), dict)
        self.assertIn('stream_network', self.report('b'))
        self.assertNotIn('stream_network', self.report('a'))


class IdempotencyAndVersionTests(LakeTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.source('a.json')
        self.items = [{'id': 'a', 'path': 'a.json'}]

    def test_rerun_with_unchanged_input_and_params_is_skipped(self):
        first = self.ingest(self.items)
        before = (self.partition('a') / 'report.json').read_bytes()
        second = self.ingest(self.items)
        self.assertEqual([r['status'] for r in first['sources']], ['committed'])
        self.assertEqual([r['status'] for r in second['sources']], ['skipped'])
        self.assertEqual(second['sources'][0]['version'], 1)
        self.assertEqual((self.partition('a') / 'report.json').read_bytes(), before)
        self.assertFalse(self.partition('a', 2).exists())

    def test_changed_input_creates_traceable_new_version_and_keeps_old(self):
        self.ingest(self.items)
        old = (self.partition('a') / 'report.json').read_bytes()
        write_json(self.a, {'cell_size_m': 1, 'elevations': [[3, 2], [1, 0]]})
        summary = self.ingest(self.items)
        self.assertEqual([r['status'] for r in summary['sources']], ['committed'])
        self.assertEqual(summary['sources'][0]['version'], 2)
        self.assertEqual((self.partition('a') / 'report.json').read_bytes(), old)
        self.assertEqual(self.report('a', 2)['total_area_m2'], 4)
        self.assertEqual(self.report('a', 1)['total_area_m2'], 48)
        catalog_versions = [(e['source_id'], e['partition_version'])
                            for e in self.catalog()['partitions']]
        self.assertEqual(catalog_versions, [('a', 1), ('a', 2)])
        self.assertTrue(lake.verify_lake(self.lake)['ok'])

    def test_changed_params_create_new_version_even_with_same_bytes(self):
        self.ingest(self.items)
        summary = self.ingest([{'id': 'a', 'path': 'a.json', 'stream_threshold': 2}])
        self.assertEqual(summary['sources'][0]['version'], 2)
        self.assertNotIn('stream_network', self.report('a', 1))
        self.assertIn('stream_network', self.report('a', 2))

    def test_unchanged_input_after_failure_is_still_skipped(self):
        self.ingest(self.items)
        bad = self.source('bad.json', raw=b'{not json')
        failed = self.ingest(self.items + [{'id': 'bad', 'path': 'bad.json'}])
        self.assertEqual([r['status'] for r in failed['sources']],
                         ['skipped', 'failed'])
        bad_result = failed['sources'][1]
        self.assertEqual(bad_result['input_sha256'], lake._digest(b'{not json'))
        self.assertFalse((self.lake / 'sources' / 'bad').exists())
        # A later manifest still leaves the committed partition as the latest.
        self.ingest(self.items, name='other.json')
        self.assertEqual(self.catalog()['partitions'][0]['partition'], 'sources/a/v1')


class FailureIsolationTests(LakeTestCase):
    def test_bad_json_failure_is_isolated_and_retried(self):
        good = self.source('good.json')
        bad = self.source('bad.json', raw=b'{"cell_size_m": 1,')
        items = [{'id': 'bad', 'path': bad.name}, {'id': 'good', 'path': good.name}]
        summary = self.ingest(items)
        statuses = {r['source_id']: r['status'] for r in summary['sources']}
        self.assertEqual(statuses, {'bad': 'failed', 'good': 'committed'})
        failed = next(r for r in summary['sources'] if r['source_id'] == 'bad')
        self.assertIsNone(failed['version'])
        # The digest identifies the bytes that failed, for traceability.
        self.assertEqual(failed['input_sha256'], lake._digest(bad.read_bytes()))
        self.assertIn('Expecting', failed['error'])
        self.assertFalse((self.lake / 'sources' / 'bad').exists())
        self.assertEqual(self.catalog()['partitions'][0]['source_id'], 'good')
        # Repair the input: only the failed source is committed.
        write_json(bad, json.loads(FIXTURE.read_text()))
        repaired = self.ingest(items)
        statuses = {r['source_id']: r['status'] for r in repaired['sources']}
        self.assertEqual(statuses, {'bad': 'committed', 'good': 'skipped'})
        self.assertTrue(lake.verify_lake(self.lake)['ok'])

    def test_missing_file_is_a_source_failure_not_a_batch_abort(self):
        self.source('good.json')
        summary = self.ingest([{'id': 'gone', 'path': 'nope.json'},
                               {'id': 'good', 'path': 'good.json'}])
        statuses = {r['source_id']: r['status'] for r in summary['sources']}
        self.assertEqual(statuses, {'gone': 'failed', 'good': 'committed'})
        self.assertEqual(summary['counts']['failed'], 1)

    def test_invalid_grid_and_bad_threshold_fail_only_that_source(self):
        self.source('ok.json')
        self.source('empty.json', {'cell_size_m': 1, 'elevations': []})
        items = [{'id': 'empty', 'path': 'empty.json'},
                 {'id': 'ok', 'path': 'ok.json'}]
        summary = self.ingest(items)
        self.assertEqual({r['source_id']: r['status'] for r in summary['sources']},
                         {'empty': 'failed', 'ok': 'committed'})
        # A bad threshold is a manifest error and aborts before any write.
        with self.assertRaises(lake.BatchError):
            self.ingest(items + [{'id': 'x', 'path': 'ok.json',
                                  'stream_threshold': 0}])

    def test_all_null_grid_commits_zero_area_and_empty_grid_fails(self):
        self.source('nulls.json', {'cell_size_m': 5, 'elevations': [[None, None]]})
        self.source('empty.json', {'cell_size_m': 5, 'elevations': []})
        summary = self.ingest([{'id': 'empty', 'path': 'empty.json'},
                               {'id': 'nulls', 'path': 'nulls.json'}])
        self.assertEqual({r['source_id']: r['status'] for r in summary['sources']},
                         {'empty': 'failed', 'nulls': 'committed'})
        report = self.report('nulls')
        self.assertEqual((report['valid_cells'], report['total_area_m2'],
                          report['cells'], report['catchments']), (0, 0, [], []))

    def test_all_null_grid_commits_zero_area_and_empty_grid_fails(self):
        self.source('nulls.json', {'cell_size_m': 5, 'elevations': [[None, None]]})
        self.source('empty.json', {'cell_size_m': 5, 'elevations': []})
        summary = self.ingest([{'id': 'empty', 'path': 'empty.json'},
                               {'id': 'nulls', 'path': 'nulls.json'}])
        self.assertEqual({r['source_id']: r['status'] for r in summary['sources']},
                         {'empty': 'failed', 'nulls': 'committed'})
        report = self.report('nulls')
        self.assertEqual((report['valid_cells'], report['total_area_m2'],
                          report['cells'], report['catchments']), (0, 0, [], []))

    def test_self_intersecting_retry_after_bad_json_preserves_good_source(self):
        self.source('good.json', {'cell_size_m': 1, 'elevations': [[2, 1]]})
        bad = self.source('bad.json', raw=b'nope')
        items = [{'id': 'bad', 'path': bad.name}, {'id': 'good', 'path': 'good.json'}]
        self.ingest(items)
        good_before = (self.partition('good') / 'report.json').read_bytes()
        # Second run with the bad source still broken must not touch good.
        self.ingest(items)
        self.assertEqual((self.partition('good') / 'report.json').read_bytes(),
                         good_before)
        self.assertFalse(self.partition('good', 2).exists())

    def test_invalid_source_ids_rejected(self):
        self.source('a.json')
        for bad in ('', '../x', 'a/b', '.hidden', 'a b', 'a' * 129,
                    'x\ny', '-dash'):
            path = self.tmp / 'm.json'
            write_json(path, {'sources': [{'id': bad, 'path': 'a.json'}]})
            with self.assertRaises(lake.BatchError):
                lake.ingest(path, self.lake)

    def test_duplicate_resolved_symlink_path_is_rejected(self):
        target = self.source('a.json')
        link = self.tmp / 'link.json'
        link.symlink_to(target)
        with self.assertRaises(lake.BatchError):
            self.ingest([{'id': 'a', 'path': 'a.json'},
                         {'id': 'b', 'path': 'link.json'}])

    def test_ragged_and_non_object_grids_fail_per_source(self):
        self.source('ok.json')
        self.source('ragged.json', {'cell_size_m': 1, 'elevations': [[1], [1, 2]]})
        self.source('floats.json', {'cell_size_m': 1, 'elevations': [[1.5]]})
        self.source('array.json', [{'cell_size_m': 1, 'elevations': [[1]]}])
        items = [{'id': 'ok', 'path': 'ok.json'},
                 {'id': 'ragged', 'path': 'ragged.json'},
                 {'id': 'floats', 'path': 'floats.json'},
                 {'id': 'array', 'path': 'array.json'}]
        summary = self.ingest(items)
        statuses = {r['source_id']: r['status'] for r in summary['sources']}
        self.assertEqual(statuses, {'ok': 'committed', 'ragged': 'failed',
                                    'floats': 'failed', 'array': 'failed'})
        self.assertEqual(summary['counts']['failed'], 3)
        for source_id in ('ragged', 'floats', 'array'):
            self.assertFalse((self.lake / 'sources' / source_id).exists())

    def test_failure_does_not_mark_a_previous_version_failed(self):
        self.source('a.json')
        self.ingest([{'id': 'a', 'path': 'a.json'}])
        self.source('a.json', raw=b'broken')
        summary = self.ingest([{'id': 'a', 'path': 'a.json'}])
        self.assertEqual(summary['sources'][0]['status'], 'failed')
        self.assertEqual(self.catalog()['partitions'][0]['partition'], 'sources/a/v1')
        self.assertTrue(lake.verify_lake(self.lake)['ok'])


class ManifestValidationTests(LakeTestCase):
    def test_duplicate_ids_abort_without_touching_lake(self):
        self.source('a.json')
        self.source('b.json')
        with self.assertRaises(lake.BatchError) as caught:
            self.ingest([{'id': 'same', 'path': 'a.json'},
                         {'id': 'same', 'path': 'b.json'}])
        self.assertIn('duplicate source id', str(caught.exception))
        self.assertFalse(self.lake.exists())

    def test_duplicate_resolved_paths_abort(self):
        self.source('a.json')
        with self.assertRaises(lake.BatchError) as caught:
            self.ingest([{'id': 'one', 'path': 'a.json'},
                         {'id': 'two', 'path': './a.json'}])
        self.assertIn('duplicate source path', str(caught.exception))

    def test_duplicate_manifest_entries_are_rejected(self):
        self.source('a.json')
        with self.assertRaises(lake.BatchError):
            self.ingest([{'id': 'a', 'path': 'a.json'},
                         {'id': 'a', 'path': 'a.json'}])
        self.assertFalse(self.lake.exists())

    def test_invalid_manifest_shapes_abort(self):
        self.source('a.json')
        cases = [
            {},
            {'sources': {}},
            {'sources': [{}]},
            {'sources': [{'id': 'x'}]},
            {'sources': [{'id': 'x', 'path': 'a.json', 'extra': 1}]},
            {'sources': [{'id': '../escape', 'path': 'a.json'}]},
            {'sources': [{'id': 'x', 'path': ''}]},
            {'sources': [{'id': 'x', 'path': 'a.json', 'stream_threshold': True}]},
            {'sources': [{'id': 'x', 'path': 'a.json', 'stream_threshold': 0}]},
        ]
        for index, document in enumerate(cases):
            path = self.tmp / ('bad-%d.json' % index)
            write_json(path, document)
            with self.assertRaises(lake.BatchError):
                lake.ingest(path, self.lake)
        self.assertFalse(self.lake.exists())

    def test_duplicate_json_keys_and_nonfinite_manifest_rejected(self):
        self.source('a.json')
        for raw in (b'{"sources": [], "sources": []}',
                    b'{"sources": [{"id": "a", "path": "a.json", "x": NaN}]}'):
            path = self.tmp / 'bad.json'
            path.write_bytes(raw)
            with self.assertRaises(lake.BatchError):
                lake.ingest(path, self.lake)

    def test_empty_manifest_creates_empty_catalog(self):
        summary = self.ingest([])
        self.assertEqual(summary['counts'],
                         {'total': 0, 'committed': 0, 'skipped': 0, 'failed': 0})
        self.assertEqual(self.catalog()['partitions'], [])
        self.assertTrue(lake.verify_lake(self.lake)['ok'])


class ExistingLakeTests(LakeTestCase):
    def test_existing_catalog_is_preserved_and_extended(self):
        for name in ('a', 'b'):
            self.source(name + '.json')
        self.ingest([{'id': 'a', 'path': 'a.json'}])
        self.ingest([{'id': 'b', 'path': 'b.json'}])
        self.assertEqual([(e['source_id'], e['partition_version'])
                          for e in self.catalog()['partitions']],
                         [('a', 1), ('b', 1)])

    def test_unrelated_files_in_lake_are_preserved(self):
        self.source('a.json')
        self.lake.mkdir()
        (self.lake / 'notes.txt').write_text('keep me')
        (self.lake / 'sources' / 'scratch').mkdir(parents=True)
        (self.lake / 'sources' / 'scratch' / 'x').write_text('keep me too')
        self.ingest([{'id': 'a', 'path': 'a.json'}])
        self.assertEqual((self.lake / 'notes.txt').read_text(), 'keep me')
        self.assertEqual((self.lake / 'sources' / 'scratch' / 'x').read_text(),
                         'keep me too')
        self.assertTrue(lake.verify_lake(self.lake)['ok'])

    def test_missing_partition_artifact_is_reported_not_overwritten(self):
        self.source('a.json')
        self.ingest([{'id': 'a', 'path': 'a.json'}])
        artifact = self.partition('a') / 'report.json'
        artifact.write_bytes(b'{"tampered": true}')
        with self.assertRaises(lake.BatchError):
            self.ingest([{'id': 'a', 'path': 'a.json'}])
        self.assertEqual(artifact.read_bytes(), b'{"tampered": true}')
        result = lake.verify_lake(self.lake)
        self.assertFalse(result['ok'])
        self.assertTrue(result['problems'])

    def test_catalog_claiming_missing_partition_aborts(self):
        self.source('a.json')
        self.ingest([{'id': 'a', 'path': 'a.json'}])
        import shutil
        shutil.rmtree(self.partition('a'))
        with self.assertRaises(lake.LakeIntegrityError):
            self.ingest([{'id': 'a', 'path': 'a.json'}])
        self.assertFalse(lake.verify_lake(self.lake)['ok'])

    def test_stale_catalog_is_repaired_without_duplicating_versions(self):
        self.source('a.json')
        self.ingest([{'id': 'a', 'path': 'a.json'}])
        (self.lake / 'catalog.json').unlink()
        summary = self.ingest([{'id': 'a', 'path': 'a.json'}])
        self.assertEqual(summary['sources'][0]['status'], 'skipped')
        self.assertEqual(summary['sources'][0]['version'], 1)
        self.assertTrue(lake.verify_lake(self.lake)['ok'])

    def test_destination_collision_fails_source_without_overwriting(self):
        self.source('a.json')
        self.ingest([{'id': 'a', 'path': 'a.json'}])
        # Simulate a lost race: the destination appears between planning and
        # commit.  Only this source fails; its old partition stays intact.
        write_json(self.source('a.json'), {'cell_size_m': 2,
                                           'elevations': [[1, 0]]})
        with mock.patch.object(lake, '_commit',
                               side_effect=lake._DestinationExists('v2 exists')):
            summary = self.ingest([{'id': 'a', 'path': 'a.json'}])
        self.assertEqual(summary['sources'][0]['status'], 'failed')
        self.assertIn('exists', summary['sources'][0]['error'])
        self.assertEqual(self.report('a')['total_area_m2'], 48)
        self.assertTrue(lake.verify_lake(self.lake)['ok'])
        # Run again: the collision is gone and a new version is committed.
        summary = self.ingest([{'id': 'a', 'path': 'a.json'}])
        self.assertEqual(summary['sources'][0]['version'], 2)


class AtomicityAndRecoveryTests(LakeTestCase):
    def test_interruption_during_commit_leaves_no_visible_partition(self):
        self.source('a.json')
        items = [{'id': 'a', 'path': 'a.json'}]
        with mock.patch.object(lake, '_rename', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.ingest(items)
        self.assertFalse(self.partition('a').exists())
        self.assertEqual(os.listdir(self.lake / '.staging'), [])
        self.assertEqual(self.catalog()['partitions'], [])
        summary = self.ingest(items)
        self.assertEqual(summary['sources'][0]['version'], 1)
        self.assertTrue(lake.verify_lake(self.lake)['ok'])

    def test_commit_oserror_is_isolated_and_old_partition_survives(self):
        self.source('a.json')
        self.ingest([{'id': 'a', 'path': 'a.json'}])
        old = self.report('a')
        write_json(self.source('a.json'), {'cell_size_m': 2, 'elevations': [[1, 0]]})
        with mock.patch.object(lake, '_rename', side_effect=OSError('disk full')):
            summary = self.ingest([{'id': 'a', 'path': 'a.json'}])
        self.assertEqual(summary['sources'][0]['status'], 'failed')
        self.assertIn('disk full', summary['sources'][0]['error'])
        self.assertEqual(self.report('a'), old)
        self.assertFalse(self.partition('a', 2).exists())
        self.assertEqual(os.listdir(self.lake / '.staging'), [])
        self.assertTrue(lake.verify_lake(self.lake)['ok'])
        # Retrying after the fault commits exactly the next version.
        summary = self.ingest([{'id': 'a', 'path': 'a.json'}])
        self.assertEqual(summary['sources'][0]['version'], 2)

    def test_commit_refuses_to_overwrite_an_existing_version_directory(self):
        self.source('a.json')
        self.ingest([{'id': 'a', 'path': 'a.json'}])
        meta = json.loads((self.partition('a') / 'meta.json').read_text())
        with self.assertRaises(lake._DestinationExists):
            lake._commit(self.lake, 'a', 1, b'{}', meta)
        self.assertEqual(self.report('a')['total_area_m2'], 48)

    def test_catalog_write_failure_leaves_partition_and_recovers(self):
        self.source('a.json')
        items = [{'id': 'a', 'path': 'a.json'}]
        real = lake._write_catalog
        calls = {'count': 0}

        def fail_after_first(lake_path, partitions):
            calls['count'] += 1
            if calls['count'] > 1:
                raise OSError('read-only')
            return real(lake_path, partitions)

        with mock.patch.object(lake, '_write_catalog', side_effect=fail_after_first):
            with self.assertRaises(OSError):
                self.ingest(items)
        # The rename committed the partition, but the catalog write after it
        # was interrupted, so the lake is valid and the catalog merely stale.
        self.assertTrue(self.partition('a').is_dir())
        self.assertEqual(self.catalog()['partitions'], [])
        self.assertFalse(lake.verify_lake(self.lake)['ok'])
        summary = self.ingest(items)
        self.assertEqual(summary['sources'][0]['status'], 'skipped')
        self.assertEqual(summary['sources'][0]['version'], 1)
        self.assertTrue(lake.verify_lake(self.lake)['ok'])

    def test_interrupted_staging_never_looks_like_a_partition(self):
        self.source('a.json')
        self.ingest([{'id': 'a', 'path': 'a.json'}])
        # Simulate a crash that left only a half-written staging directory.
        leftover = self.lake / '.staging' / 'commit-leftover'
        leftover.mkdir(parents=True)
        (leftover / 'report.json').write_bytes(b'{')
        result = lake.verify_lake(self.lake)
        self.assertTrue(result['ok'])
        self.assertEqual(result['partitions'], 1)
        summary = self.ingest([{'id': 'a', 'path': 'a.json'}])
        self.assertEqual(summary['sources'][0]['status'], 'skipped')

    def test_partial_version_directory_is_an_integrity_error(self):
        self.source('a.json')
        self.ingest([{'id': 'a', 'path': 'a.json'}])
        incomplete = self.lake / 'sources' / 'a' / 'v2'
        incomplete.mkdir()
        (incomplete / 'report.json').write_bytes(b'{}')
        self.assertFalse(lake.verify_lake(self.lake)['ok'])
        with self.assertRaises(lake.LakeIntegrityError):
            self.ingest([{'id': 'a', 'path': 'a.json'}])
        # The incomplete directory is not deleted or mistaken for success.
        self.assertTrue((incomplete / 'report.json').exists())

    def test_interruption_after_partition_before_catalog_is_repaired(self):
        self.source('a.json')
        self.ingest([{'id': 'a', 'path': 'a.json'}])
        catalog_before = (self.lake / 'catalog.json').read_bytes()
        (self.lake / 'catalog.json').unlink()
        result = lake.verify_lake(self.lake)
        self.assertFalse(result['ok'])
        self.assertIn('catalog.json is missing', result['problems'][0])
        lake.rebuild_catalog(self.lake)
        self.assertEqual((self.lake / 'catalog.json').read_bytes(), catalog_before)


class RebuildAndChainTests(LakeTestCase):
    def test_rebuild_is_byte_identical_and_order_stable(self):
        for source_id in ('m', 'a', 'z'):
            self.source(source_id + '.json')
        self.ingest([{'id': s, 'path': s + '.json'} for s in ('z', 'm', 'a')])
        before = (self.lake / 'catalog.json').read_bytes()
        for _ in range(3):
            lake.rebuild_catalog(self.lake)
            self.assertEqual((self.lake / 'catalog.json').read_bytes(), before)
        catalog = self.catalog()
        self.assertEqual([e['source_id'] for e in catalog['partitions']], ['a', 'm', 'z'])

    def test_rebuild_rejects_corrupt_partition_without_touching_catalog(self):
        self.source('a.json')
        self.ingest([{'id': 'a', 'path': 'a.json'}])
        before = (self.lake / 'catalog.json').read_bytes()
        (self.partition('a') / 'report.json').write_bytes(b'{}')
        with self.assertRaises(lake.BatchError):
            lake.rebuild_catalog(self.lake)
        self.assertEqual((self.lake / 'catalog.json').read_bytes(), before)

    def test_tampered_catalog_does_not_claim_false_completion(self):
        self.source('a.json')
        self.ingest([{'id': 'a', 'path': 'a.json'}])
        catalog_path = self.lake / 'catalog.json'
        document = json.loads(catalog_path.read_text())
        document['partitions'][0]['artifact_sha256'] = '0' * 64
        write_json(catalog_path, document)
        result = lake.verify_lake(self.lake)
        self.assertFalse(result['ok'])
        self.assertFalse(result['catalog_matches'])
        self.assertTrue(any('disagrees' in p for p in result['problems']))
        # A batch refuses to build on the corrupt catalog, leaving it intact.
        before = catalog_path.read_bytes()
        with self.assertRaises(lake.LakeIntegrityError):
            self.ingest([{'id': 'a', 'path': 'a.json'}])
        self.assertEqual(catalog_path.read_bytes(), before)

    def test_version_numbers_sort_numerically(self):
        self.source('a.json')
        self.ingest([{'id': 'a', 'path': 'a.json'}])
        # Fabricate a valid v10 by copying v1's bytes and adjusting metadata.
        tenth = self.lake / 'sources' / 'a' / 'v10'
        import shutil
        shutil.copytree(self.partition('a'), tenth)
        meta = json.loads((tenth / 'meta.json').read_text())
        meta['partition_version'] = 10
        write_json(tenth / 'meta.json', meta)
        lake.rebuild_catalog(self.lake)
        write_json(self.source('a.json'), {'cell_size_m': 1,
                                           'elevations': [[9, 8]]})
        summary = self.ingest([{'id': 'a', 'path': 'a.json'}])
        self.assertEqual(summary['sources'][0]['version'], 11)


class CliTests(LakeTestCase):
    def run_cli(self, *args):
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))
        return subprocess.run([sys.executable, '-m', 'terrain_flow.cli', *args],
                              capture_output=True, text=True, env=env)

    def test_batch_cli_exit_codes_and_summary(self):
        self.source('good.json')
        self.source('bad.json', raw=b'{')
        manifest = self.manifest([{'id': 'good', 'path': 'good.json'},
                                  {'id': 'bad', 'path': 'bad.json'}])
        run = self.run_cli('batch', '--manifest', str(manifest),
                           '--lake', str(self.lake))
        self.assertEqual(run.returncode, 1, run.stderr)
        summary = json.loads(run.stdout)
        self.assertEqual(summary['counts']['failed'], 1)
        verify = self.run_cli('verify', '--lake', str(self.lake))
        self.assertEqual(verify.returncode, 0, verify.stderr)
        self.assertTrue(json.loads(verify.stdout)['ok'])
        rebuild = self.run_cli('rebuild-catalog', '--lake', str(self.lake))
        self.assertEqual(rebuild.returncode, 0, rebuild.stderr)

    def test_batch_cli_writes_result_and_reports_manifest_errors(self):
        self.source('a.json')
        manifest = self.manifest([{'id': 'a', 'path': 'a.json'}])
        result = self.tmp / 'summary.json'
        run = self.run_cli('batch', '--manifest', str(manifest),
                           '--lake', str(self.lake), '--result', str(result))
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(result.read_text()), json.loads(run.stdout))
        bad = self.tmp / 'bad-manifest.json'
        bad.write_text('{"sources": [{"id": "a", "path": "a.json"}, '
                       '{"id": "a", "path": "a.json"}]}')
        failed = self.run_cli('batch', '--manifest', str(bad),
                              '--lake', str(self.lake))
        self.assertEqual(failed.returncode, 2)
        self.assertIn('duplicate source id', failed.stderr)

    def test_single_file_cli_still_works_after_batch_dispatch(self):
        out = self.tmp / 'report.json'
        run = self.run_cli('--source', str(FIXTURE), '--output', str(out))
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(out.read_text()),
                         drain(json.loads(FIXTURE.read_text())))
        self.assertNotIn('stream_network', json.loads(out.read_text()))


if __name__ == '__main__':
    unittest.main()
