"""Batch ingestion of independent terrain JSON sources into a partitioned lake.

Each source is computed independently with the existing single-source D8
pipeline.  A successful source becomes an immutable partition::

    <lake>/sources/<source-id>/v<N>/report.json
    <lake>/sources/<source-id>/v<N>/meta.json

``report.json`` is exactly the single-source report produced by
:func:`terrain_flow.drainage.drain`.  ``meta.json`` records the input digest,
parameters and artifact digest.  ``<lake>/catalog.json`` is a deterministic,
rebuildable materialised view of every committed partition::

    {"format": "terrain-flow-lake-catalog", "schema_version": 1,
     "partitions": [{"source_id": ..., "partition_version": N,
                     "partition": "sources/<id>/v<N>", "input_sha256": ...,
                     "params": {"stream_threshold": N|null},
                     "pipeline_version": N, "artifact_sha256": ...,
                     "artifact_bytes": N, "status": "committed"}]}

The catalog contains only committed content.  Per-attempt outcomes, including
isolated source failures, are returned in the batch summary (and written to
``--result`` when requested); they are not lake content and are therefore not
part of the catalog that can be rebuilt from disk.

Commit atomicity is one directory rename: a complete partition is prepared in
``<lake>/.staging/`` and then renamed to ``sources/<id>/v<N>`` while the
destination does not exist.  A process interruption can therefore leave a
staging directory, but never a visible partition with a partial artifact: a
``vN`` directory only ever appears with both files fully written.  The catalog
is written atomically after the partition and is derived data, so it may lag
behind the partitions but never leads them.

The report itself is never held in memory: it is streamed record by record
into the staging file while its SHA-256 and byte count are accumulated, and
those accumulated values become the metadata.  Existing artifacts are checked
by a chunked digest, so lake scanning also runs in bounded memory.  A failure
while the report is being streamed removes the staging directory; the old
partition and the catalog are left untouched.

A single batch is assumed to have a single publisher, matching the existing
single-file output contract; no cross-process locking is provided.
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from .drainage import analyze
from .io import fields, integer, pairs, parse, serialize, text
from .streaming import Sink, chunked_digest, write_document

# Bump when a change to the computation could alter report bytes for the same
# input and parameters.  A bump makes every source commit a new version.
PIPELINE_VERSION = 1

CATALOG_FORMAT = 'terrain-flow-lake-catalog'
CATALOG_VERSION = 1
META_FORMAT = 'terrain-flow-partition'
META_VERSION = 1
BATCH_FORMAT = 'terrain-flow-batch-result'
BATCH_VERSION = 1

SOURCE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')
VERSION_DIR = re.compile(r'v([1-9][0-9]*)\Z')
SHA256 = re.compile(r'[0-9a-f]{64}\Z')

PARTITION_FIELDS = ('source_id partition_version partition input_sha256 params '
                    'pipeline_version artifact_sha256 artifact_bytes status')
META_FIELDS = ('format schema_version pipeline_version source_id '
               'partition_version input_sha256 params status artifact')
ARTIFACT_FIELDS = 'path sha256 bytes'
CATALOG_FIELDS = 'format schema_version partitions'
PARAMS_FIELDS = {'stream_threshold'}


class BatchError(ValueError):
    """A fatal manifest or lake error; the batch makes no source writes."""


class LakeIntegrityError(BatchError):
    """An existing lake is inconsistent with what it claims to contain."""


class _DestinationExists(Exception):
    """A partition version directory unexpectedly already exists."""


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _source_id(value):
    if not isinstance(value, str) or not SOURCE_ID.fullmatch(value):
        raise BatchError('source id must match %s' % SOURCE_ID.pattern)
    return value


def _params(value):
    if not isinstance(value, dict) or set(value) != PARAMS_FIELDS:
        raise ValueError('expected params fields: stream_threshold')
    threshold = value['stream_threshold']
    if threshold is not None:
        threshold = integer(threshold, 1, 10 ** 9)
    return {'stream_threshold': threshold}


def _valid_digest(value):
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def _partition_rel(source_id, version):
    return 'sources/%s/v%d' % (source_id, version)


def _artifact_json(raw):
    """Parse a lake artifact, allowing reports larger than the 8 MB input cap."""
    def invalid(value):
        raise ValueError('non-finite JSON number: ' + value)
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


# ---------------------------------------------------------------------------
# Manifest


def read_manifest(path):
    """Validate an explicit manifest and return resolved source entries.

    Paths are resolved relative to the manifest's directory.  Duplicate IDs
    and duplicate resolved paths are manifest errors, so they abort the whole
    batch before the lake is touched.
    """
    manifest = Path(path)
    try:
        raw = manifest.read_bytes()
    except OSError as error:
        raise BatchError('cannot read manifest: %s' % error) from error
    try:
        document = parse(raw)
    except ValueError as error:
        raise BatchError('invalid manifest: %s' % error) from error
    if not isinstance(document, dict) or set(document) != {'sources'}:
        raise BatchError('manifest must have exactly: sources')
    items = document['sources']
    if not isinstance(items, list):
        raise BatchError('manifest sources must be an array')
    base = manifest.resolve().parent
    entries = []
    ids = {}
    paths = {}
    for index, item in enumerate(items):
        where = 'manifest source %d' % index
        if not isinstance(item, dict):
            raise BatchError(where + ': expected object')
        keys = set(item)
        if keys not in ({'id', 'path'}, {'id', 'path', 'stream_threshold'}):
            raise BatchError(where + ': expected fields: id path [stream_threshold]')
        try:
            source_id = _source_id(item['id'])
        except ValueError as error:
            raise BatchError(where + ': ' + str(error)) from error
        try:
            source_path = text(item['path'])
        except ValueError as error:
            raise BatchError(where + ': ' + str(error)) from error
        threshold = None
        if 'stream_threshold' in item:
            try:
                threshold = integer(item['stream_threshold'], 1, 10 ** 9)
            except ValueError as error:
                raise BatchError(where + ': stream_threshold must be a positive integer') from error
        try:
            resolved = (base / source_path).resolve()
        except (OSError, RuntimeError) as error:
            raise BatchError(where + ': cannot resolve path: %s' % error) from error
        if source_id in ids:
            raise BatchError('duplicate source id: %s' % source_id)
        if resolved in paths:
            raise BatchError('duplicate source path: %s (ids %s and %s)'
                             % (resolved, paths[resolved], source_id))
        ids[source_id] = resolved
        paths[resolved] = source_id
        entries.append({'source_id': source_id, 'path': resolved,
                        'params': {'stream_threshold': threshold}})
    return entries


# ---------------------------------------------------------------------------
# Metadata, catalog and partition scanning


def _meta(document, source_id=None, version=None):
    try:
        fields(document, META_FIELDS)
        if document['format'] != META_FORMAT:
            raise ValueError('unexpected metadata format')
        if integer(document['schema_version'], 1, 10 ** 9) != META_VERSION:
            raise ValueError('unsupported metadata schema version')
        pipeline_version = integer(document['pipeline_version'], 1, 10 ** 9)
        stored_id = _source_id(document['source_id'])
        stored_version = integer(document['partition_version'], 1, 10 ** 9)
        if not _valid_digest(document['input_sha256']):
            raise ValueError('invalid input sha256')
        params = _params(document['params'])
        if document['status'] != 'committed':
            raise ValueError('partition status is not committed')
        artifact = document['artifact']
        if not isinstance(artifact, dict):
            raise ValueError('artifact must be an object')
        fields(artifact, ARTIFACT_FIELDS)
        if artifact['path'] != 'report.json':
            raise ValueError('artifact path must be report.json')
        if not _valid_digest(artifact['sha256']):
            raise ValueError('invalid artifact sha256')
        byte_count = integer(artifact['bytes'], 0, 10 ** 12)
    except ValueError as error:
        if isinstance(error, LakeIntegrityError):
            raise
        raise LakeIntegrityError('invalid partition metadata: %s' % error) from error
    if source_id is not None and stored_id != source_id:
        raise LakeIntegrityError('partition metadata source_id does not match its directory')
    if version is not None and stored_version != version:
        raise LakeIntegrityError('partition metadata version does not match its directory')
    return {'format': META_FORMAT, 'schema_version': META_VERSION,
            'pipeline_version': pipeline_version,
            'source_id': stored_id, 'partition_version': stored_version,
            'input_sha256': document['input_sha256'], 'params': params,
            'status': 'committed',
            'artifact': {'path': 'report.json',
                         'sha256': artifact['sha256'],
                         'bytes': byte_count}}


def _catalog_entry(meta):
    return {'source_id': meta['source_id'],
            'partition_version': meta['partition_version'],
            'partition': _partition_rel(meta['source_id'], meta['partition_version']),
            'input_sha256': meta['input_sha256'],
            'params': meta['params'],
            'pipeline_version': meta['pipeline_version'],
            'artifact_sha256': meta['artifact']['sha256'],
            'artifact_bytes': meta['artifact']['bytes'],
            'status': 'committed'}


def _scan(lake, parse_reports):
    """Return ``{(source_id, version): catalog_entry}`` for a lake.

    Every visible ``vN`` partition directory is treated as committed and is
    fully checked against its metadata and artifact digest.  Entries that do
    not look like partitions are ignored so unrelated files are preserved,
    but a directory that looks like a partition while being incomplete is an
    integrity error: the lake must never need to guess.
    """
    lake = Path(lake)
    root = lake / 'sources'
    if not root.exists() and not root.is_symlink():
        return {}
    if root.is_symlink() or not root.is_dir():
        raise LakeIntegrityError('lake sources entry is not a plain directory')
    partitions = {}
    for source_dir in sorted(root.iterdir(), key=lambda p: p.name):
        if source_dir.is_symlink() or not source_dir.is_dir():
            continue
        source_id = source_dir.name
        if SOURCE_ID.fullmatch(source_id) is None:
            continue
        for item in sorted(source_dir.iterdir(), key=lambda p: p.name):
            match = VERSION_DIR.fullmatch(item.name)
            if match is None:
                continue
            version = int(match.group(1))
            if item.is_symlink() or not item.is_dir():
                raise LakeIntegrityError('partition is not a plain directory: %s' % item)
            meta_path = item / 'meta.json'
            if meta_path.is_symlink() or not meta_path.is_file():
                raise LakeIntegrityError('partition metadata is missing: %s' % item)
            try:
                meta = _meta(parse(meta_path.read_bytes()), source_id, version)
            except OSError as error:
                raise LakeIntegrityError('cannot read partition metadata %s: %s'
                                         % (item, error)) from error
            except ValueError as error:
                raise LakeIntegrityError('partition metadata is unreadable %s: %s'
                                         % (item, error)) from error
            artifact = item / meta['artifact']['path']
            if artifact.is_symlink() or not artifact.is_file():
                raise LakeIntegrityError('partition artifact is missing: %s' % item)
            try:
                digest, byte_count = chunked_digest(artifact)
            except OSError as error:
                raise LakeIntegrityError('cannot read partition artifact %s: %s'
                                         % (item, error)) from error
            # Bounded chunked digest: no report-sized buffer is held here.
            if byte_count != meta['artifact']['bytes']:
                raise LakeIntegrityError('partition artifact size mismatch: %s' % item)
            if digest != meta['artifact']['sha256']:
                raise LakeIntegrityError('partition artifact digest mismatch: %s' % item)
            if parse_reports:
                try:
                    raw = artifact.read_bytes()
                except OSError as error:
                    raise LakeIntegrityError('cannot read partition artifact %s: %s'
                                             % (item, error)) from error
                try:
                    report = _artifact_json(raw)
                except ValueError as error:
                    raise LakeIntegrityError('partition report is not valid JSON: %s: %s'
                                             % (item, error)) from error
                if not isinstance(report, dict):
                    raise LakeIntegrityError('partition report is not a JSON object: %s' % item)
                del raw
            partitions[(source_id, version)] = _catalog_entry(meta)
    return partitions


def _catalog_entry_valid(entry):
    try:
        fields(entry, PARTITION_FIELDS)
        source_id = _source_id(entry['source_id'])
        version = integer(entry['partition_version'], 1, 10 ** 9)
        if entry['partition'] != _partition_rel(source_id, version):
            raise ValueError('partition path does not match source id and version')
        if not _valid_digest(entry['input_sha256']):
            raise ValueError('invalid input sha256')
        params = _params(entry['params'])
        pipeline_version = integer(entry['pipeline_version'], 1, 10 ** 9)
        if not _valid_digest(entry['artifact_sha256']):
            raise ValueError('invalid artifact sha256')
        byte_count = integer(entry['artifact_bytes'], 0, 10 ** 12)
        if entry['status'] != 'committed':
            raise ValueError('catalog status is not committed')
    except ValueError as error:
        if isinstance(error, LakeIntegrityError):
            raise
        raise LakeIntegrityError('invalid catalog partition: %s' % error) from error
    return {'source_id': source_id, 'partition_version': version,
            'partition': entry['partition'], 'input_sha256': entry['input_sha256'],
            'params': params, 'pipeline_version': pipeline_version,
            'artifact_sha256': entry['artifact_sha256'],
            'artifact_bytes': byte_count, 'status': 'committed'}


def read_catalog(lake):
    """Read and validate ``catalog.json``; return ``None`` when absent."""
    path = Path(lake) / 'catalog.json'
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise LakeIntegrityError('catalog is not a plain file')
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise LakeIntegrityError('cannot read catalog: %s' % error) from error
    try:
        document = _artifact_json(raw)
        fields(document, CATALOG_FIELDS)
        if document['format'] != CATALOG_FORMAT:
            raise ValueError('unexpected catalog format')
        if integer(document['schema_version'], 1, 10 ** 9) != CATALOG_VERSION:
            raise ValueError('unsupported catalog schema version')
        if not isinstance(document['partitions'], list):
            raise ValueError('catalog partitions must be an array')
        partitions = [_catalog_entry_valid(entry) for entry in document['partitions']]
    except ValueError as error:
        if isinstance(error, LakeIntegrityError):
            raise
        raise LakeIntegrityError('invalid catalog: %s' % error) from error
    keys = [(e['source_id'], e['partition_version']) for e in partitions]
    if len(keys) != len(set(keys)):
        raise LakeIntegrityError('catalog contains duplicate partitions')
    return partitions


def _check_catalog(catalog, partitions):
    """Reject a catalog that disagrees with the partitions visible on disk.

    A catalog entry without a partition claims a commit that is not there, and
    an entry whose digests differ from the partition bytes is corrupt: both
    abort the batch.  A catalog that merely omits partitions is stale but
    recoverable (disk is authoritative); it is repaired by the next write.
    """
    if catalog is None:
        return
    for entry in catalog:
        key = (entry['source_id'], entry['partition_version'])
        visible = partitions.get(key)
        if visible is None:
            raise LakeIntegrityError('catalog references missing partition: %s'
                                     % entry['partition'])
        if entry != visible:
            raise LakeIntegrityError('catalog disagrees with partition: %s'
                                     % entry['partition'])


def _catalog_bytes(partitions):
    document = {'format': CATALOG_FORMAT, 'schema_version': CATALOG_VERSION,
                'partitions': [partitions[key] for key in sorted(partitions)]}
    return serialize(document).encode('utf-8')


# ---------------------------------------------------------------------------
# Atomic writes


def _write_synced(path, data):
    with open(path, 'wb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _replace(path, data):
    """Atomically replace ``path`` with ``data``, unless bytes are unchanged."""
    path = Path(path)
    if path.is_symlink():
        raise LakeIntegrityError('refusing to replace symlink: %s' % path)
    try:
        if path.read_bytes() == data:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='.tmp-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return True


def _rename(source, destination):
    """Indirection kept as a seam for interruption tests."""
    os.rename(source, destination)


def _commit(lake, source_id, version, write_report, meta):
    """Publish a partition with a single directory rename.

    ``write_report(handle)`` streams the report bytes into the staging file
    and returns ``(sha256, byte_count)``; the bytes are accumulated while
    writing, so no report-sized buffer is ever held.  Returns the catalog
    entry for the newly visible partition.  The staging directory is created
    outside ``sources/`` so a partial write can never be mistaken for a
    committed partition.
    """
    sources = Path(lake) / 'sources'
    source_dir = sources / source_id
    if source_dir.is_symlink():
        raise _DestinationExists('source directory is a symlink: %s' % source_dir)
    source_dir.mkdir(parents=True, exist_ok=True)
    destination = source_dir / ('v%d' % version)
    if destination.exists() or destination.is_symlink():
        raise _DestinationExists('partition already exists: %s' % destination)
    staging = Path(lake) / '.staging'
    staging.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='commit-', dir=staging))
    try:
        descriptor, report_name = tempfile.mkstemp(prefix='report-', dir=temporary)
        with os.fdopen(descriptor, 'wb') as handle:
            digest, byte_count = write_report(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(report_name, temporary / 'report.json')
        meta['artifact']['sha256'] = digest
        meta['artifact']['bytes'] = byte_count
        _write_synced(temporary / 'meta.json', serialize(meta).encode('utf-8'))
        _rename(temporary, destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return _catalog_entry(meta)


def _write_catalog(lake, partitions):
    return _replace(Path(lake) / 'catalog.json', _catalog_bytes(partitions))


# ---------------------------------------------------------------------------
# Batch


def _failure_message(error):
    return str(error) or error.__class__.__name__


def ingest(manifest_path, lake_path):
    """Run one batch and return a deterministic JSON-ready summary.

    Manifest errors and lake integrity errors raise :class:`BatchError`
    before any source is written.  Source failures (unreadable or invalid
    input, computation errors, a destination collision) are isolated: the
    remaining sources continue, the failure is recorded, and nothing is
    published for that source.  A source is skipped only when its latest
    committed partition has the same input digest, parameters and pipeline
    version; any change commits a new version and never overwrites an old one.

    No source-level exception is swallowed as an unexpected error.  The
    catalog is rewritten after each committed source, so an interruption
    leaves the catalog in sync with whatever partitions are already visible.
    A catalog write failure is lake infrastructure, not a source failure; it
    propagates rather than being recorded against an already committed source.
    """
    try:
        manifest_bytes = Path(manifest_path).read_bytes()
    except OSError as error:
        raise BatchError('cannot read manifest: %s' % error) from error
    entries = read_manifest(manifest_path)
    lake = Path(lake_path)
    if lake.is_symlink():
        raise BatchError('lake symlink is not allowed')
    if lake.exists() and not lake.is_dir():
        raise BatchError('lake is not a directory: %s' % lake)
    lake.mkdir(parents=True, exist_ok=True)
    # Validate all existing state before writing anything, and write a catalog
    # that already matches the visible partitions.  Every later catalog write
    # is only ever a superset, so an interruption at any point leaves the lake
    # consistent with its catalog.
    partitions = _scan(lake, parse_reports=False)
    _check_catalog(read_catalog(lake), partitions)
    _write_catalog(lake, partitions)

    results = []
    for entry in sorted(entries, key=lambda e: e['source_id']):
        source_id = entry['source_id']
        params = entry['params']
        digest = None
        changed = False
        try:
            raw = entry['path'].read_bytes()
            digest = _digest(raw)
            versions = [v for (sid, v) in partitions if sid == source_id]
            current = partitions[(source_id, max(versions))] if versions else None
            reusable = (current is not None
                        and current['input_sha256'] == digest
                        and current['params'] == params
                        and current['pipeline_version'] == PIPELINE_VERSION)
            if reusable:
                results.append(_result(current, params, 'skipped'))
                continue
            data = parse(raw)
            del raw
            analysis = analyze(data, params['stream_threshold'])
            del data
            document = analysis.document()
            version = max(versions) + 1 if versions else 1
            meta = {'format': META_FORMAT, 'schema_version': META_VERSION,
                    'pipeline_version': PIPELINE_VERSION,
                    'source_id': source_id, 'partition_version': version,
                    'input_sha256': digest, 'params': params,
                    'status': 'committed',
                    'artifact': {'path': 'report.json',
                                 'sha256': None, 'bytes': None}}

            def write_report(handle):
                sink = Sink(handle, hashlib.sha256())
                write_document(sink, document, indent=2, newline=True)
                return sink.digest_value(), sink.bytes

            committed = _commit(lake, source_id, version, write_report, meta)
            del document, analysis
            partitions[(source_id, version)] = committed
            results.append(_result(committed, params, 'committed'))
            changed = True
        except (ValueError, OSError, KeyError, TypeError, OverflowError,
                RecursionError, _DestinationExists) as error:
            results.append({'source_id': source_id, 'status': 'failed',
                            'version': None, 'input_sha256': digest,
                            'params': params, 'artifact_sha256': None,
                            'error': _failure_message(error)})
        # Persist the catalog only when a partition was committed.  A failure
        # here is a lake infrastructure error, not a source failure, so it
        # propagates instead of being recorded against an already committed
        # source (which would be a false failure and a duplicate result).
        if changed:
            _write_catalog(lake, partitions)
    # Also repair a stale catalog when no source produced a new partition.
    _write_catalog(lake, partitions)
    counts = {'total': len(results),
              'committed': sum(r['status'] == 'committed' for r in results),
              'skipped': sum(r['status'] == 'skipped' for r in results),
              'failed': sum(r['status'] == 'failed' for r in results)}
    return {'format': BATCH_FORMAT, 'schema_version': BATCH_VERSION,
            'manifest_sha256': _digest(manifest_bytes),
            'counts': counts, 'sources': results}


def _result(entry, params, status):
    return {'source_id': entry['source_id'], 'status': status,
            'version': entry.get('partition_version'),
            'input_sha256': entry['input_sha256'], 'params': params,
            'artifact_sha256': entry.get('artifact_sha256'), 'error': None}


# ---------------------------------------------------------------------------
# Verification and catalog rebuild


def verify_lake(lake_path):
    """Check artifacts and catalog consistency without modifying the lake."""
    lake = Path(lake_path)
    try:
        partitions = _scan(lake, parse_reports=True)
    except (BatchError, OSError) as error:
        return {'ok': False, 'partitions': 0, 'catalog_matches': False,
                'problems': [_failure_message(error)]}
    problems = []
    catalog = None
    try:
        catalog = read_catalog(lake)
    except (BatchError, OSError) as error:
        problems.append(_failure_message(error))
    if (Path(lake) / 'catalog.json').exists():
        if catalog is not None:
            try:
                _check_catalog(catalog, partitions)
            except BatchError as error:
                problems.append(_failure_message(error))
    else:
        problems.append('catalog.json is missing')
    matches = False
    if catalog is not None:
        catalog_keys = {(e['source_id'], e['partition_version']) for e in catalog}
        missing = sorted(set(partitions) - catalog_keys)
        extra = sorted(catalog_keys - set(partitions))
        if missing or extra:
            problems.append('catalog partitions differ from lake: missing %s, extra %s'
                            % (missing, extra))
        else:
            matches = not problems
    return {'ok': not problems, 'partitions': len(partitions),
            'catalog_matches': matches, 'problems': problems}


def rebuild_catalog(lake_path):
    """Rebuild ``catalog.json`` from committed partitions.

    Raises :class:`BatchError` if any partition is invalid; the existing
    catalog is left untouched in that case.
    """
    lake = Path(lake_path)
    partitions = _scan(lake, parse_reports=True)
    _write_catalog(lake, partitions)
    return {'ok': True, 'partitions': len(partitions),
            'catalog_matches': True, 'problems': []}
