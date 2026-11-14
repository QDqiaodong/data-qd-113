"""Peak-memory and publication-boundary tests for the CLI production path.

Every measured run is a fresh interpreter.  ``tracemalloc`` is started before
any business module is imported, stdout is redirected to a real file, and the
peak is reported on stderr after the command finishes.  The measured runs never
call the object APIs (``drain``/``run``): they exercise the installed command
line entry points only.  Only this process (the assertions, the frozen-baseline
comparison) imports ``analyze`` directly.

The three stress grids are 100k-cell constructions: a flat plateau, a sawtooth
grid with many confluences, and a half-null sparse grid.  Each runs without and
with ``--stream-threshold 1``, both as a single file and through ``batch`` into
a lake that already holds committed partitions.
"""

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from terrain_flow.drainage import analyze
from terrain_flow.io import publish_document
from terrain_flow.streaming import Sink, write_document

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
LIMIT = 64 * 2 ** 20

MEASURE = textwrap.dedent('''
    """Run one CLI command under tracemalloc; report the peak on stderr."""
    import json, sys, tracemalloc

    root = sys.argv[1]
    cli_args = sys.argv[2:]
    sys.path.insert(0, root)
    tracemalloc.start(1)                      # before importing any business module
    from terrain_flow.cli import main as cli_main
    code = cli_main(cli_args)
    current, peak = tracemalloc.get_traced_memory()
    sys.stderr.write('\\n' + json.dumps({'peak_bytes': peak,
                                         'current_bytes': current}) + '\\n')
    raise SystemExit(code)
''')


def write_json(path, value):
    path.write_text(json.dumps(value, separators=(',', ':')), encoding='utf-8')
    return path


def plateau_grid():
    """99856 cells, all one elevation: every cell is its own outlet."""
    width = height = 316
    return {'cell_size_m': 1,
            'elevations': [[7] * width for _ in range(height)]}


def zigzag_grid():
    """100000 cells with ridges and many confluences; no flat plateaus."""
    height, width = 250, 400
    return {'cell_size_m': 2,
            'elevations': [[1000 - ((r * 37 + c * 53) % 400) for c in range(width)]
                           for r in range(height)]}


def sparse_grid():
    """100000 cells, half of them null, with sparse valid islands."""
    import random
    height, width = 400, 250
    rng = random.Random(7)
    return {'cell_size_m': 3,
            'elevations': [[None if (r + c) % 2 else rng.randint(-50, 300)
                            for c in range(width)] for r in range(height)]}


class MeasuredRun(unittest.TestCase):
    """Shared harness; each test gets a scratch directory and a fresh process."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.measure = self.tmp / 'measure.py'
        self.measure.write_text(MEASURE, encoding='utf-8')

    def tearDown(self):
        self._tmp.cleanup()

    def measured(self, *args, cwd=None):
        """Run the CLI under tracemalloc; return ``(code, peak, stdout_bytes)``."""
        with tempfile.TemporaryFile() as stdout:
            process = subprocess.run(
                [sys.executable, '-I', str(self.measure), str(SRC), *args],
                stdout=stdout, stderr=subprocess.PIPE, cwd=str(cwd or self.tmp))
            stdout.seek(0)
            printed = stdout.read()
        report = process.stderr.decode('utf-8', 'replace').strip().splitlines()
        self.assertTrue(report, process.stderr)
        try:
            peak = json.loads(report[-1])['peak_bytes']
        except (ValueError, KeyError):
            self.fail('measurement harness failed: %r' % process.stderr.decode())
        return process.returncode, peak, printed

    def check_peak(self, label, code, peak):
        self.assertEqual(code, 0, label)
        self.assertLessEqual(peak, LIMIT,
                             '%s peak %.1f MiB exceeds 64 MiB'
                             % (label, peak / 2 ** 20))


class SingleFileMemoryTests(MeasuredRun):
    def grid(self, name):
        path = self.tmp / (name + '.json')
        write_json(path, {'plateau': plateau_grid, 'zigzag': zigzag_grid,
                          'sparse': sparse_grid}[name]())
        return path

    def test_each_grid_under_each_threshold_peaks_below_limit(self):
        for name in ('plateau', 'zigzag', 'sparse'):
            source = self.grid(name)
            for threshold in (None, 1):
                output = self.tmp / ('%s-%s.json' % (name, threshold))
                args = ['--source', str(source), '--output', str(output)]
                if threshold is not None:
                    args += ['--stream-threshold', str(threshold)]
                code, peak, stdout = self.measured(*args)
                with self.subTest(grid=name, threshold=threshold):
                    self.check_peak('%s/%s' % (name, threshold), code, peak)
                    # The file and stdout carry the same document.
                    raw = output.read_bytes()
                    written = json.loads(raw)
                    printed = json.loads(stdout.decode('utf-8'))
                    self.assertEqual(printed, written)
                    self.assertEqual(written['valid_cells'] > 0, True)
                    # Re-serialising the parsed document reproduces the file
                    # bytes exactly (indent=2, sorted keys, trailing newline).
                    expected = json.dumps(
                        written, ensure_ascii=False, sort_keys=True,
                        indent=2, allow_nan=False) + '\n'
                    self.assertEqual(raw.decode('utf-8'), expected)
                    self.assertEqual(stdout.decode('utf-8'),
                                     json.dumps(written, ensure_ascii=False,
                                                sort_keys=True, allow_nan=False)
                                     + '\n')

    def test_streamed_file_is_byte_identical_to_object_serialisation(self):
        # The production path never materialises the report; compare it with
        # the frozen serialisation settings on a modest grid instead.
        source = self.tmp / 'small.json'
        write_json(source, {'cell_size_m': 3,
                            'elevations': [[8, 4, 9], [7, 2, 6], [1, 5, 3]]})
        output = self.tmp / 'report.json'
        code, peak, _ = self.measured('--source', str(source),
                                      '--output', str(output),
                                      '--stream-threshold', '2')
        self.check_peak('small', code, peak)
        raw = output.read_bytes()
        self.assertTrue(raw.endswith(b'\n'))
        document = json.loads(raw)
        expected = json.dumps(document, ensure_ascii=False, sort_keys=True,
                              indent=2, allow_nan=False) + '\n'
        self.assertEqual(raw.decode('utf-8'), expected)


class BatchMemoryTests(MeasuredRun):
    def test_batch_with_existing_partitions_peaks_below_limit(self):
        grids = {'plateau': plateau_grid, 'zigzag': zigzag_grid,
                 'sparse': sparse_grid}
        manifest_dir = self.tmp / 'sources'
        manifest_dir.mkdir()
        for name, factory in grids.items():
            write_json(manifest_dir / (name + '.json'), factory())
        lake = self.tmp / 'lake'

        # Establish a lake that already holds two committed partitions, one of
        # them with a stream network.
        seed = manifest_dir / 'seed.json'
        write_json(seed, {'sources': [
            {'id': 'seed-a', 'path': 'plateau.json'},
            {'id': 'seed-b', 'path': 'zigzag.json', 'stream_threshold': 1}]})
        code, peak, _ = self.measured('batch', '--manifest', str(seed),
                                      '--lake', str(lake))
        self.check_peak('batch seed', code, peak)

        # Re-running the identical manifest skips; no new partition.
        code, peak, _ = self.measured('batch', '--manifest', str(seed),
                                      '--lake', str(lake))
        self.check_peak('batch rerun', code, peak)
        self.assertEqual([p.name for p in (lake / 'sources' / 'seed-a').iterdir()],
                         ['v1'])

        # Then ingest every grid under both thresholds into that existing lake.
        for name in ('plateau', 'zigzag', 'sparse'):
            source = manifest_dir / (name + '.json')
            for threshold in (None, 1):
                item = {'id': name, 'path': name + '.json'}
                if threshold is not None:
                    item['stream_threshold'] = threshold
                manifest = manifest_dir / ('%s-%s.json' % (name, threshold))
                write_json(manifest, {'sources': [item]})
                code, peak, _ = self.measured(
                    'batch', '--manifest', str(manifest), '--lake', str(lake))
                with self.subTest(grid=name, threshold=threshold):
                    self.check_peak('batch existing/%s/%s' % (name, threshold),
                                    code, peak)

        # Two updated sources commit new versions in the same batch.
        changed = manifest_dir / 'changed.json'
        write_json(changed, {'sources': [
            {'id': 'seed-a', 'path': 'plateau.json', 'stream_threshold': 1},
            {'id': 'seed-b', 'path': 'zigzag.json'},
            {'id': 'sparse', 'path': 'sparse.json', 'stream_threshold': 1}]})
        code, peak, _ = self.measured('batch', '--manifest', str(changed),
                                      '--lake', str(lake))
        self.check_peak('batch changed', code, peak)
        self.assertEqual([p.name for p in (lake / 'sources' / 'seed-a').iterdir()],
                         ['v1', 'v2'])


def sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def cli(args, cwd, env=None):
    environment = dict(os.environ)
    environment['PYTHONPATH'] = str(SRC)
    if env:
        environment.update(env)
    return subprocess.run([sys.executable, '-m', 'terrain_flow.cli', *args],
                          capture_output=True, cwd=str(cwd), env=environment)


class PartitionPublicationTests(unittest.TestCase):
    """Digests, idempotent skips and half-written staging failure semantics."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.source = write_json(self.tmp / 'a.json', plateau_grid())
        self.lake = self.tmp / 'lake'
        self.manifest = write_json(self.tmp / 'manifest.json',
                                   {'sources': [{'id': 'a', 'path': 'a.json'}]})

    def tearDown(self):
        self._tmp.cleanup()

    def partition(self, version=1):
        return self.lake / 'sources' / 'a' / ('v%d' % version)

    def test_artifact_digest_and_length_match_streamed_bytes(self):
        run = cli(['batch', '--manifest', str(self.manifest),
                   '--lake', str(self.lake)], self.tmp)
        self.assertEqual(run.returncode, 0, run.stderr)
        raw = (self.partition() / 'report.json').read_bytes()
        meta = json.loads((self.partition() / 'meta.json').read_text())
        self.assertEqual(meta['artifact']['bytes'], len(raw))
        self.assertEqual(meta['artifact']['sha256'], sha256_bytes(raw))
        self.assertEqual(meta['input_sha256'], sha256_bytes(self.source.read_bytes()))
        verify = cli(['verify', '--lake', str(self.lake)], self.tmp)
        self.assertEqual(verify.returncode, 0, verify.stderr)
        self.assertTrue(json.loads(verify.stdout)['ok'])

    def test_rerun_skips_without_rewriting_and_keeps_version(self):
        first = cli(['batch', '--manifest', str(self.manifest),
                     '--lake', str(self.lake)], self.tmp)
        self.assertEqual(first.returncode, 0, first.stderr)
        before = (self.partition() / 'report.json').read_bytes()
        second = cli(['batch', '--manifest', str(self.manifest),
                      '--lake', str(self.lake)], self.tmp)
        self.assertEqual(second.returncode, 0, second.stderr)
        summary = json.loads(second.stdout)
        self.assertEqual(summary['sources'][0]['status'], 'skipped')
        self.assertEqual(summary['sources'][0]['version'], 1)
        self.assertEqual((self.partition() / 'report.json').read_bytes(), before)
        self.assertFalse(self.partition(2).exists())

    def test_partial_staging_write_failure_preserves_old_partition(self):
        first = cli(['batch', '--manifest', str(self.manifest),
                     '--lake', str(self.lake)], self.tmp)
        self.assertEqual(first.returncode, 0, first.stderr)
        old_report = (self.partition() / 'report.json').read_bytes()
        old_meta = (self.partition() / 'meta.json').read_bytes()
        old_catalog = (self.lake / 'catalog.json').read_bytes()

        # Change the input so a new version would be planned, then inject a
        # failure halfway through streaming the report into staging.
        write_json(self.source, {'cell_size_m': 2, 'elevations': [[5, 4], [3, 0]]})
        injector = self.tmp / 'inject.py'
        injector.write_text(textwrap.dedent('''
            import sys
            sys.path.insert(0, %r)
            import terrain_flow.streaming as streaming
            real = streaming.Sink.write
            calls = {'n': 0}
            def failing(sink, text):
                calls['n'] += 1
                if calls['n'] == 3:
                    raise OSError('injected staging failure')
                return real(sink, text)
            streaming.Sink.write = failing
            from terrain_flow.cli import main
            raise SystemExit(main(sys.argv[1:]))
        ''' % str(SRC)), encoding='utf-8')
        process = subprocess.run(
            [sys.executable, str(injector), 'batch', '--manifest',
             str(self.manifest), '--lake', str(self.lake)],
            capture_output=True, cwd=str(self.tmp))
        # A source failure is isolated and reported with exit 1.
        self.assertEqual(process.returncode, 1, process.stderr)
        summary = json.loads(process.stdout)
        self.assertEqual(summary['sources'][0]['status'], 'failed')
        self.assertIn('injected staging failure', summary['sources'][0]['error'])
        # The old partition and catalog are untouched; nothing new is visible.
        self.assertEqual((self.partition() / 'report.json').read_bytes(), old_report)
        self.assertEqual((self.partition() / 'meta.json').read_bytes(), old_meta)
        self.assertEqual((self.lake / 'catalog.json').read_bytes(), old_catalog)
        self.assertFalse(self.partition(2).exists())
        self.assertEqual(os.listdir(self.lake / '.staging'), [])
        verify = cli(['verify', '--lake', str(self.lake)], self.tmp)
        self.assertEqual(verify.returncode, 0, verify.stderr)
        # Retrying after the fault publishes exactly version 2.
        retry = cli(['batch', '--manifest', str(self.manifest),
                     '--lake', str(self.lake)], self.tmp)
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(json.loads(retry.stdout)['sources'][0]['version'], 2)
        self.assertEqual((self.partition() / 'report.json').read_bytes(), old_report)


class FrozenBaselineTests(unittest.TestCase):
    """Small random grids, byte-for-byte against the pre-streaming report."""

    def test_streamed_report_and_stdout_match_frozen_bytes(self):
        fixture = json.loads((ROOT / 'fixtures/drainage-baseline.json').read_text(
            encoding='utf-8'))
        self.assertTrue(fixture['cases'])
        for index, case in enumerate(fixture['cases']):
            data = {'cell_size_m': case['cell_size_m'],
                    'elevations': case['elevations']}
            with self.subTest(case=index, threshold=case['threshold']):
                analysis = analyze(data, case['threshold'])
                document = analysis.document()
                with tempfile.TemporaryDirectory() as td:
                    output = Path(td) / 'report.json'
                    publish_document(output, document)
                    self.assertEqual(output.read_bytes(),
                                     case['report'].encode('utf-8'))
                # stdout uses the compact settings; the same document is
                # re-iterated, proving the record factories are restartable.
                buffer = io.BytesIO()
                write_document(Sink(buffer), document, indent=None, newline=True)
                self.assertEqual(buffer.getvalue(), case['stdout'].encode('utf-8'))
                # The object API still returns the identical report.
                self.assertEqual(analysis.report(),
                                 json.loads(case['report']))


if __name__ == '__main__':
    unittest.main()