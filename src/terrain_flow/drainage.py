"""Integer-grid strict-descent D8 with exact squared-slope comparisons.

The object API :func:`drain` returns the complete report dictionary used by
existing callers and tests.  The CLI production path uses :func:`analyze` and
:class:`Analysis`, which keep the graph in flat integer arrays and yield the
per-cell, catchment and stream-network records one at a time.  That avoids
building the full list of per-cell dictionaries or a complete report string in
memory; report bytes are unchanged.
"""

from array import array

from .io import fields, integer, array as array_value
from .streaming import Records

ELEVATION_MIN = -10000
ELEVATION_MAX = 10000
MAX_CELLS = 100000
SENTINEL = 32767  # outside the elevation range; marks no-data in array('h')

_NEIGHBOURS = ((-1, -1), (-1, 0), (-1, 1), (0, -1),
               (0, 1), (1, -1), (1, 0), (1, 1))


def _stream_records(factory):
    return Records(factory)


class Analysis:
    """Compact, immutable D8 state with lazy report records.

    ``elevation``, ``target``, ``accumulation`` and ``outlet`` are flat arrays
    indexed by ``row * width + col``.  Targets are ``-1`` for an outlet and
    ``SENTINEL`` elevations mark no-data cells.
    """

    __slots__ = ('height', 'width', 'cell_size_m', 'elevation', 'target',
                 'accumulation', 'outlet', 'outlet_cells', 'valid_cells',
                 'high_order', 'threshold', 'network')

    def __init__(self, height, width, cell_size_m, elevation, target,
                 accumulation, outlet, outlet_cells, valid_cells, high_order):
        self.height = height
        self.width = width
        self.cell_size_m = cell_size_m
        self.elevation = elevation
        self.target = target
        self.accumulation = accumulation
        self.outlet = outlet
        self.outlet_cells = outlet_cells
        self.valid_cells = valid_cells
        self.high_order = high_order
        self.threshold = None
        self.network = None

    # -- stream network ---------------------------------------------------

    def with_stream_network(self, threshold):
        if threshold is None:
            return self
        self.threshold = threshold
        self.network = _Network(self, threshold)
        return self

    # -- lazy records -----------------------------------------------------

    def _cell_record(self, index):
        width = self.width
        downstream = self.target[index]
        count = self.accumulation[index]
        outlet = self.outlet[index]
        size = self.cell_size_m
        return {
            'row': index // width,
            'col': index % width,
            'elevation_m': self.elevation[index],
            'downstream': (None if downstream < 0 else
                           [downstream // width, downstream % width]),
            'upstream_cells': count,
            'contributing_area_m2': count * size * size,
            'outlet': [outlet // width, outlet % width],
        }

    def _cells(self):
        elevation = self.elevation
        width = self.width
        for index in range(self.height * width):
            if elevation[index] != SENTINEL:
                yield self._cell_record(index)

    def _catchments(self):
        width = self.width
        size = self.cell_size_m
        outlet = self.outlet
        outlet_cells = self.outlet_cells
        for index in range(self.height * width):
            count = outlet_cells[index]
            if count:
                yield {'outlet': [index // width, index % width],
                       'cells': count, 'area_m2': count * size * size}

    def document(self):
        """Return the small report descriptor with streaming record arrays."""
        size = self.cell_size_m
        document = {
            'shape': [self.height, self.width],
            'cell_size_m': size,
            'valid_cells': self.valid_cells,
            'total_area_m2': self.valid_cells * size * size,
            'cells': _stream_records(self._cells),
            'catchments': _stream_records(self._catchments),
        }
        if self.network is not None:
            document['stream_network'] = self.network.document()
        return document

    def report(self):
        """Materialise the complete object report (the historical API)."""
        report = {
            'shape': [self.height, self.width],
            'cell_size_m': self.cell_size_m,
            'valid_cells': self.valid_cells,
            'total_area_m2': self.valid_cells * self.cell_size_m ** 2,
            'cells': list(self._cells()),
            'catchments': list(self._catchments()),
        }
        if self.network is not None:
            report['stream_network'] = {
                'threshold': self.network.threshold,
                'cell_size_m': self.cell_size_m,
                'cell_count': self.network.cell_count,
                'edge_count': self.network.edge_count,
                'nodes': list(self.network.nodes()),
                'chains': list(self.network.chains()),
            }
        return report


class _Network:
    """Threshold stream network over fixed D8 targets, in compact arrays."""

    __slots__ = ('analysis', 'threshold', 'member', 'head', 'next_edge',
                 'incoming', 'order', 'is_node', 'node_ids', 'node_order',
                 'cell_count', 'edge_count')

    def __init__(self, analysis, threshold):
        self.analysis = analysis
        self.threshold = threshold
        elevation = analysis.elevation
        target = analysis.target
        accumulation = analysis.accumulation
        n = len(elevation)
        member = bytearray(n)
        count = 0
        for index in range(n):
            if elevation[index] != SENTINEL and accumulation[index] >= threshold:
                member[index] = 1
                count += 1
        self.member = member
        self.cell_count = count

        head = array('i', [-1]) * n
        next_edge = array('i', [-1]) * n
        incoming = array('i', [0]) * n
        edge_count = 0
        for index in range(n):
            if member[index] and target[index] >= 0:
                downstream = target[index]
                next_edge[index] = head[downstream]
                head[downstream] = index
                incoming[downstream] += 1
                edge_count += 1
        self.head = head
        self.next_edge = next_edge
        self.incoming = incoming
        self.edge_count = edge_count

        # Strahler order over in-network tributaries.  ``high_order`` lists
        # cells by decreasing elevation, so every tributary is resolved first.
        order = array('i', [0]) * n
        for index in analysis.high_order:
            if not member[index]:
                continue
            highest = 0
            ties = 0
            upstream = head[index]
            if upstream < 0:
                order[index] = 1
                continue
            while upstream >= 0:
                value = order[upstream]
                if value > highest:
                    highest = value
                    ties = 1
                elif value == highest:
                    ties += 1
                upstream = next_edge[upstream]
            order[index] = highest + (1 if ties >= 2 else 0)
        self.order = order

        is_node = bytearray(n)
        node_order = array('i')
        for index in range(n):
            if member[index] and (incoming[index] != 1 or target[index] < 0):
                is_node[index] = 1
                node_order.append(index)
        self.is_node = is_node
        self.node_order = node_order
        node_ids = array('i', [-1]) * n
        for node_id, index in enumerate(node_order):
            node_ids[index] = node_id
        self.node_ids = node_ids

    def nodes(self):
        width = self.analysis.width
        accumulation = self.analysis.accumulation
        order = self.order
        for node_id, index in enumerate(self.node_order):
            yield {'id': node_id,
                   'row': index // width,
                   'col': index % width,
                   'upstream_cells': accumulation[index],
                   'strahler': order[index]}

    def chains(self):
        width = self.analysis.width
        target = self.analysis.target
        is_node = self.is_node
        node_ids = self.node_ids
        order = self.order
        size = self.analysis.cell_size_m
        chain_id = 0
        for start in self.node_order:
            if target[start] < 0:
                continue  # isolated stream cell: node only
            orthogonal = diagonal = 0
            previous = start
            current = target[start]
            while True:
                dr = abs(previous // width - current // width)
                dc = abs(previous % width - current % width)
                if dr == 1 and dc == 1:
                    diagonal += 1
                else:
                    orthogonal += 1
                if is_node[current]:
                    break
                previous = current
                current = target[current]
            yield {'id': chain_id,
                   'start_node': node_ids[start],
                   'end_node': node_ids[current],
                   'start': [start // width, start % width],
                   'end': [current // width, current % width],
                   'orthogonal_steps': orthogonal,
                   'diagonal_steps': diagonal,
                   # Exact length in metres; never evaluated as a float.
                   'length_expression': '%d*(%d+%d*sqrt(2))'
                                        % (size, orthogonal, diagonal),
                   'strahler': order[start]}
            chain_id += 1

    def document(self):
        return {'threshold': self.threshold,
                'cell_size_m': self.analysis.cell_size_m,
                'cell_count': self.cell_count,
                'edge_count': self.edge_count,
                'nodes': _stream_records(self.nodes),
                'chains': _stream_records(self.chains)}


def analyze(data, stream_threshold=None):
    """Validate a terrain document and return compact :class:`Analysis` state."""
    fields(data, 'cell_size_m elevations')
    size = integer(data['cell_size_m'], 1, 10000)
    if stream_threshold is not None and (type(stream_threshold) != int
                                         or stream_threshold < 1):
        raise ValueError('stream threshold must be a positive integer')
    grid = array_value(data['elevations'])
    if not grid or not isinstance(grid[0], list) or not grid[0]:
        raise ValueError('nonempty rectangular grid required')
    height = len(grid)
    width = len(grid[0])
    if height * width > MAX_CELLS:
        raise ValueError('grid exceeds 100000 cells')
    total = height * width
    elevation = array('h', [SENTINEL]) * total
    valid = 0
    for r, row in enumerate(grid):
        if not isinstance(row, list) or len(row) != width:
            raise ValueError('ragged grid')
        base = r * width
        for c, value in enumerate(row):
            if value is not None:
                elevation[base + c] = integer(value, ELEVATION_MIN, ELEVATION_MAX)
                valid += 1

    target = array('i', [-1]) * total
    for r in range(height):
        base = r * width
        for c in range(width):
            index = base + c
            z = elevation[index]
            if z == SENTINEL:
                continue
            best_key = None
            best = -1
            for dr, dc in _NEIGHBOURS:
                nr = r + dr
                nc = c + dc
                if nr < 0 or nr >= height or nc < 0 or nc >= width:
                    continue
                other = nr * width + nc
                other_z = elevation[other]
                if other_z == SENTINEL or other_z >= z:
                    continue
                rise = z - other_z
                # Exact positive-slope comparison.  The original compares
                # -Fraction(rise**2, run**2); multiplying by 2 clears the only
                # possible denominator (2 for a diagonal) and leaves the
                # ordering unchanged.
                if dr and dc:
                    key = -(rise * rise)
                else:
                    key = -(2 * rise * rise)
                if best_key is None or key < best_key or (
                        key == best_key and other < best):
                    best_key = key
                    best = other
            target[index] = best

    accumulation = array('i', [0]) * total
    for index in range(total):
        if elevation[index] != SENTINEL:
            accumulation[index] = 1
    high_order = [index for index in range(total)
                  if elevation[index] != SENTINEL]
    high_order.sort(key=lambda index: (-elevation[index], index))
    for index in high_order:
        downstream = target[index]
        if downstream >= 0:
            accumulation[downstream] += accumulation[index]

    outlet = array('i', [-1]) * total
    for index in reversed(high_order):
        downstream = target[index]
        outlet[index] = index if downstream < 0 else outlet[downstream]

    outlet_cells = array('i', [0]) * total
    for index in high_order:
        outlet_cells[outlet[index]] += 1

    analysis = Analysis(height, width, size, elevation, target, accumulation,
                        outlet, outlet_cells, valid, high_order)
    if stream_threshold is not None:
        analysis.with_stream_network(stream_threshold)
    return analysis


def drain(data, stream_threshold=None):
    """Return the complete report dictionary for a terrain document."""
    return analyze(data, stream_threshold).report()
