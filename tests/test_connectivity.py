import json
import os
import random
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from terrain_flow.connectivity import (
    build_index,
    load_document,
    parse_grid,
    parse_queries,
    run,
)

ROOT = Path(__file__).resolve().parents[1]
NEIGHBOURS = ((-1, -1), (-1, 0), (-1, 1), (0, -1),
              (0, 1), (1, -1), (1, 0), (1, 1))


def reference_components(elevations, height, width, level):
    """Brute-force 8-neighbour flood fill of cells active at ``level``."""
    seen = [False] * len(elevations)
    components = []
    for start, z in enumerate(elevations):
        if seen[start] or z is None or z > level:
            continue
        stack = [start]
        seen[start] = True
        members = []
        while stack:
            index = stack.pop()
            members.append(index)
            r, c = divmod(index, width)
            for dr, dc in NEIGHBOURS:
                nr, nc = r + dr, c + dc
                if not (0 <= nr < height and 0 <= nc < width):
                    continue
                other = nr * width + nc
                if seen[other] or elevations[other] is None \
                        or elevations[other] > level:
                    continue
                seen[other] = True
                stack.append(other)
        components.append(sorted(members))
    return sorted(components, key=lambda members: members[0])


class HandBuiltForestTests(unittest.TestCase):
    def grid(self, elevations, cell_size=1):
        return {'cell_size_m': cell_size, 'elevations': elevations}

    def build(self, data, queries):
        elevations, height, width, area = parse_grid(data)
        triples = parse_queries({'queries': queries}, height, width)
        return build_index(elevations, height, width, area, triples), width

    def test_equal_level_basins_merge_into_one_multiway_node(self):
        # Four single-cell basins at level 1; the whole level-2 cross joins
        # all four basins simultaneously.  The result must be one 4-way node,
        # not a chain of artificial binary merges.
        data = self.grid([[1, 2, 1],
                          [2, 2, 2],
                          [1, 2, 1]])
        result, _ = self.build(data, [{'row': 1, 'col': 1, 'level': 2}])
        forest = result['forest']
        leaves = [n for n in forest if not n['children']]
        merges = [n for n in forest if n['children']]
        self.assertEqual(len(leaves), 4)
        self.assertEqual(len(merges), 1)
        merge = merges[0]
        self.assertEqual(merge['event_level'], 2)
        self.assertEqual(sorted(merge['children']), [0, 1, 2, 3])
        self.assertEqual(result['roots'], [merge['id']])
        answer = result['queries'][0]
        self.assertEqual(answer['node_id'], merge['id'])
        self.assertEqual(answer['area_cells'], 9)

    def test_parallel_saddles_do_not_make_a_binary_chain(self):
        # Two level-3 bridge cells independently connect the same two
        # components within one batch: still a single binary merge node.
        data = self.grid([[1, 3, 1],
                          [1, 3, 1]])
        result, _ = self.build(data, [{'row': 0, 'col': 1, 'level': 3}])
        merges = [n for n in result['forest'] if n['children']]
        self.assertEqual(len(merges), 1)
        self.assertEqual(len(merges[0]['children']), 2)
        self.assertEqual(result['queries'][0]['area_cells'], 6)

    def test_multi_cell_group_accumulates_all_old_neighbours(self):
        # The level-2 group spans several cells; only its second cell touches
        # a second old basin.  The gathered neighbour set must accumulate
        # across every cell of the group (regression for stale neighbour set).
        data = self.grid([[1, 2, 3],
                          [4, 2, 5],
                          [4, 2, 1]])
        result, _ = self.build(
            data, [{'row': r, 'col': c, 'level': 5}
                   for r in range(3) for c in range(3)])
        # Both level-1 leaves must eventually be children of one merge tree.
        leaves = [n['id'] for n in result['forest'] if not n['children']]
        self.assertEqual(len(leaves), 2)
        all_children = {child for n in result['forest']
                        for child in n['children']}
        self.assertTrue(set(leaves).issubset(all_children))
        # At level 2 the vertical group joins the top-left leaf and the
        # bottom-right leaf through one merge node (fan-out 2, no chain).
        merge_at_two = [n for n in result['forest']
                        if n['event_level'] == 2 and n['children']]
        self.assertEqual(len(merge_at_two), 1)
        self.assertEqual(sorted(merge_at_two[0]['children']), leaves)

    def test_expansion_creates_no_new_node(self):
        # A basin grows one cell at a time through four distinct levels; the
        # original leaf must remain the only node naming the component.
        data = self.grid([[4, 3, 2, 1]])
        result, width = self.build(
            data, [{'row': 0, 'col': c, 'level': 4} for c in range(4)])
        self.assertEqual(result['node_count'], 1)
        node = result['forest'][0]
        self.assertEqual(node['event_level'], 1)
        self.assertEqual(node['min_cell'], [0, 3])
        self.assertEqual(node['children'], [])
        for answer in result['queries']:
            self.assertEqual(answer['node_id'], 0)
        self.assertEqual(result['queries'][-1]['area_cells'], 4)

    def test_diagonal_cells_connect_across_null_orthogonal_neighbours(self):
        data = self.grid([[1, None], [None, 1]])
        result, _ = self.build(
            data, [{'row': 0, 'col': 0, 'level': 1},
                   {'row': 1, 'col': 1, 'level': 1}])
        self.assertEqual(result['node_count'], 1)
        for answer in result['queries']:
            self.assertEqual(answer['node_id'], 0)
            self.assertEqual(answer['area_cells'], 2)

    def test_orthogonal_null_barrier_blocks_connection(self):
        # Diagonal touch exists, but the pair is separated by nulls in a
        # grid where diagonal neighbours are not actually adjacent here.
        data = self.grid([[1, None, 1],
                          [None, None, None],
                          [1, None, 1]])
        result, _ = self.build(
            data, [{'row': r, 'col': c, 'level': 1}
                   for r in (0, 2) for c in (0, 2)])
        self.assertEqual(result['node_count'], 4)
        self.assertEqual(len({q['node_id'] for q in result['queries']}), 4)

    def test_negative_elevations_and_exact_storage(self):
        data = self.grid([[-5, -10, None], [-7, -10, -3]], cell_size=3)
        result, _ = self.build(
            data, [{'row': 0, 'col': 1, 'level': -10},
                   {'row': 1, 'col': 2, 'level': -4},
                   {'row': 1, 'col': 2, 'level': -3},
                   {'row': 0, 'col': 2, 'level': 0}])
        answers = result['queries']
        self.assertEqual(answers[0]['area_cells'], 2)
        self.assertEqual(answers[0]['storage_m3'], 0)
        self.assertIsNone(answers[1])      # -3 cell still inactive at -4
        self.assertIsNone(answers[3])      # null cell is always an obstacle
        # sum of elevations = -35 over 5 cells; storage = (h*n - sum z)*area
        self.assertEqual(answers[2]['area_cells'], 5)
        self.assertEqual(answers[2]['storage_m3'], (-3 * 5 - (-35)) * 9)
        self.assertEqual(answers[2]['area_m2'], 45)
        # Every later activation only expanded the single leaf.
        self.assertEqual(result['node_count'], 1)
        self.assertEqual(result['forest'][0]['event_level'], -10)

    def test_all_null_grid_is_an_empty_forest(self):
        data = self.grid([[None, None], [None, None]])
        result, _ = self.build(
            data, [{'row': 0, 'col': 0, 'level': 10000}])
        self.assertEqual(result['forest'], [])
        self.assertEqual(result['roots'], [])
        self.assertEqual(result['node_count'], 0)
        self.assertIsNone(result['queries'][0])

    def test_query_below_first_activation_returns_null(self):
        data = self.grid([[0, 5], [5, 5]])
        result, _ = self.build(
            data, [{'row': 0, 'col': 0, 'level': -1},
                   {'row': 0, 'col': 1, 'level': 4},
                   {'row': 0, 'col': 1, 'level': 5}])
        self.assertIsNone(result['queries'][0])
        self.assertIsNone(result['queries'][1])
        self.assertEqual(result['queries'][2]['node_id'], 0)
        self.assertEqual(result['queries'][2]['area_cells'], 4)

    def test_storage_hand_values_on_two_level_basin(self):
        data = self.grid([[2, 2, 2], [2, 1, 2], [2, 2, 2]], cell_size=2)
        result, _ = self.build(
            data, [{'row': 1, 'col': 1, 'level': 1},
                   {'row': 1, 'col': 1, 'level': 3}])
        at1, at3 = result['queries']
        self.assertEqual(at1['storage_m3'], 0)
        # At level 3: one cell contributes (3-1)=2, eight cells (3-2)=1.
        self.assertEqual(at3['storage_m3'], (2 + 8) * 4)
        self.assertEqual(at3['area_cells'], 9)
        # The level-2 batch merely expands the leaf born at level 1.
        self.assertEqual(result['node_count'], 1)
        self.assertEqual(result['forest'][0]['event_level'], 1)

    def test_node_ids_sorted_by_level_then_min_coordinate(self):
        data = self.grid([[5, 1, 5],
                          [5, 5, 5],
                          [5, 1, 5],
                          [5, 5, 5]])
        result, width = self.build(
            data, [{'row': r, 'col': c, 'level': 5}
                   for r in range(4) for c in range(3)])
        keys = [(n['event_level'], n['min_cell']) for n in result['forest']]
        self.assertEqual(keys, sorted(keys))
        for node in result['forest']:
            for child in node['children']:
                self.assertLess(result['forest'][child]['event_level'],
                                node['event_level'])
                self.assertLess(child, node['id'])
        # No unary nodes: merges always join at least two children.
        self.assertTrue(all(len(n['children']) != 1
                            for n in result['forest']))


class RandomFloodReferenceTests(unittest.TestCase):
    """Cross-check the index against an independent per-level flood fill."""

    def build_and_check(self, seed, height, width, cell_size):
        rng = random.Random(seed)
        rows = []
        for _ in range(height):
            row = []
            for _ in range(width):
                roll = rng.randrange(7)
                row.append(None if roll == 0 else roll - 3)  # -3..3 or null
            rows.append(row)
        data = {'cell_size_m': cell_size, 'elevations': rows}
        elevations, h, w, area = parse_grid(data)
        distinct_levels = sorted({z for z in elevations if z is not None})
        # Query every cell at every distinct level plus levels just outside
        # the active range; shuffle to prove original order is preserved.
        probe_levels = ([min(distinct_levels, default=0) - 1]
                        + distinct_levels
                        + [max(distinct_levels, default=0) + 1])
        queries = [{'row': r, 'col': c, 'level': level}
                   for level in probe_levels
                   for r in range(h) for c in range(w)]
        rng.shuffle(queries)
        triples = parse_queries({'queries': queries}, h, w)
        result = build_index(elevations, h, w, area, triples)
        forest = result['forest']

        # Structural invariants of the forest.
        keys = [(n['event_level'], n['min_cell']) for n in forest]
        self.assertEqual(keys, sorted(keys))
        self.assertTrue(all(len(n['children']) != 1 for n in forest))
        for node in forest:
            for child in node['children']:
                self.assertLess(forest[child]['event_level'],
                                node['event_level'])
        # The roots are exactly the components at the highest event level.
        top_components = reference_components(
            elevations, h, w, max(distinct_levels, default=0))
        if distinct_levels:
            self.assertEqual(len(result['roots']), len(top_components))

        seen_nodes = set()
        for level in probe_levels:
            components = reference_components(elevations, h, w, level)
            cell_to_component = {}
            for cid, members in enumerate(components):
                for index in members:
                    cell_to_component[index] = cid
            # Pull the index answer for every cell at this level.
            answers_at_level = {}
            for q_index, (qlevel, r, c) in enumerate(triples):
                if qlevel == level:
                    answers_at_level[r * w + c] = result['queries'][q_index]
            for index, z in enumerate(elevations):
                answer = answers_at_level[index]
                if z is None or z > level:
                    self.assertIsNone(answer)
                    continue
                self.assertIsNotNone(answer)
                cid = cell_to_component[index]
                members = components[cid]
                # Every cell of one flood component names the same tree node,
                # and distinct components name distinct nodes.
                node_ids = {answers_at_level[other]['node_id']
                            for other in members}
                self.assertEqual(node_ids, {answer['node_id']})
                # Area and exact storage against the brute-force component.
                self.assertEqual(answer['area_cells'], len(members))
                self.assertEqual(answer['area_m2'], len(members) * area)
                expected_storage = sum(level - elevations[other]
                                       for other in members) * area
                self.assertEqual(answer['storage_m3'], expected_storage)
                node = forest[answer['node_id']]
                # A node's min_cell is frozen at its birth event: it must lie
                # inside the current component and be the minimum of the
                # component as it existed at that event level.
                self.assertIn(node_min_index := (node['min_cell'][0] * w
                                                 + node['min_cell'][1]),
                              members)
                birth_components = reference_components(
                    elevations, h, w, node['event_level'])
                birth_members = next(group for group in birth_components
                                     if node_min_index in group)
                self.assertEqual(node['min_cell'],
                                 list(divmod(min(birth_members), w)))
                self.assertLessEqual(node['event_level'], level)
                seen_nodes.add(answer['node_id'])

        # Every forest node is observable at some queried level.
        self.assertEqual(seen_nodes, set(range(len(forest))))

        # Replay the birth event of every merge node against the flood fill at
        # the previous level: its children name the absorbed old components.
        event_levels = distinct_levels
        for node in forest:
            if not node['children']:
                continue
            level = node['event_level']
            previous = max((L for L in event_levels if L < level),
                           default=None)
            before = reference_components(
                elevations, h, w, previous) if previous is not None else []
            child_ids = set(node['children'])
            absorbed = set()
            for members in before:
                probe = members[0]
                r, c = divmod(probe, w)
                qidx = next(i for i, (ql, qr, qc) in enumerate(triples)
                            if ql == previous and qr == r and qc == c)
                absorbed.add(result['queries'][qidx]['node_id'])
            # The merge's children must be a subset of the previously living
            # nodes that end up inside this component.
            self.assertTrue(child_ids.issubset(absorbed))

    def test_random_small_grids_match_flood_fill(self):
        for seed in range(40):
            h = 1 + seed % 6
            w = 1 + (seed * 3) % 7
            self.build_and_check(seed, h, w, 1 + seed % 5)

    def test_random_dense_grid_with_repeated_elevations(self):
        # Few distinct elevations force wide, equal-level batches.
        rng = random.Random(909)
        h = w = 12
        rows = [[rng.choice([0, 0, 1, 1, 2, None]) for _ in range(w)]
                for _ in range(h)]
        data = {'cell_size_m': 4, 'elevations': rows}
        elevations, height, width, area = parse_grid(data)
        queries = [{'row': r, 'col': c, 'level': level}
                   for level in range(-1, 3)
                   for r in range(h) for c in range(w)]
        triples = parse_queries({'queries': queries}, height, width)
        result = build_index(elevations, height, width, area, triples)
        for level in range(-1, 3):
            components = reference_components(elevations, height, width, level)
            owner = {}
            for cid, members in enumerate(components):
                for index in members:
                    owner[index] = cid
            for q_index, (qlevel, r, c) in enumerate(triples):
                if qlevel != level:
                    continue
                index = r * width + c
                answer = result['queries'][q_index]
                if elevations[index] is None or elevations[index] > level:
                    self.assertIsNone(answer)
                    continue
                members = components[owner[index]]
                self.assertEqual(answer['area_cells'], len(members))
                self.assertEqual(answer['area_m2'], len(members) * 16)
                self.assertEqual(answer['storage_m3'],
                                 sum(level - elevations[o] for o in members) * 16)


class ScaleTests(unittest.TestCase):
    def test_100k_cells_and_queries_run_near_linear_without_recursion(self):
        # A 100000-cell snake whose elevation rises along its length: a naive
        # recursive DSU/DFS would blow the stack, and the sweep must stay well
        # inside the O((V+Q) log(V+Q)) budget.
        height, width = 1, 100000
        elevations = [i % 97 for i in range(width)]
        rng = random.Random(7)
        queries = [(rng.randrange(-10000, 10001), 0, rng.randrange(width))
                   for _ in range(100000)]
        start = time.perf_counter()
        result = build_index(elevations, height, width, 1, queries)
        elapsed = time.perf_counter() - start
        self.assertEqual(len(result['queries']), 100000)
        self.assertLess(elapsed, 30.0)
        # At the maximum level every cell is one component (the snake has no
        # null), so a single root names the whole grid.
        top = reference_components(elevations, height, width, 10000)
        self.assertEqual(len(top), 1)
        self.assertEqual(len(result['roots']), 1)
        # Storage summed across the final component matches the direct sum.
        final_query = max(range(len(queries)),
                          key=lambda i: queries[i][0])
        level = queries[final_query][0]
        answer = result['queries'][final_query]
        if level >= max(elevations):
            self.assertEqual(answer['area_cells'], width)
            self.assertEqual(answer['storage_m3'],
                             sum(level - z for z in elevations))

    def test_100k_grid_100k_queries_worst_case_merge_load(self):
        # Level-0 cells sit on every second row/column so none are 8-neighbours:
        # they form thousands of isolated leaves.  The single level-1 batch then
        # joins them all through one high-fan-out merge node.
        height = width = 316  # 99856 <= 100000 cells
        elevations = [0 if (r % 2 == 0 and c % 2 == 0) else 1
                      for r in range(height) for c in range(width)]
        leaves = sum(1 for z in elevations if z == 0)
        rng = random.Random(11)
        queries = [(rng.choice([-1, 0, 1]), rng.randrange(height),
                    rng.randrange(width)) for _ in range(100000)]
        start = time.perf_counter()
        result = build_index(elevations, height, width, 25, queries)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 30.0)
        self.assertEqual(len(result['queries']), 100000)
        # A query is null exactly when the cell is not active at its level:
        # level -1 activates nothing, and level 0 activates only the spaced
        # zero cells while the level-1 cells stay inactive.
        expected_nulls = sum(
            level < 0 or (level == 0 and elevations[r * width + c] != 0)
            for level, r, c in queries)
        self.assertEqual(sum(q is None for q in result['queries']),
                         expected_nulls)
        merge_nodes = [n for n in result['forest'] if n['children']]
        self.assertEqual(len(merge_nodes), 1)
        self.assertEqual(len(merge_nodes[0]['children']), leaves)
        self.assertEqual(merge_nodes[0]['event_level'], 1)
        self.assertEqual(len([n for n in result['forest']
                              if not n['children']]), leaves)
        self.assertEqual(result['roots'], [merge_nodes[0]['id']])
        # A level-1 query anywhere reaches the single final component.
        level_one = [q for (level, _r, _c), q in
                     zip(queries, result['queries']) if level == 1]
        self.assertTrue(level_one)
        self.assertTrue(all(q['node_id'] == merge_nodes[0]['id']
                            and q['area_cells'] == height * width
                            for q in level_one))


class ValidationTests(unittest.TestCase):
    def build(self, data, queries):
        elevations, h, w, area = parse_grid(data)
        return build_index(elevations, h, w, area,
                           parse_queries({'queries': queries}, h, w))

    def test_invalid_grids_are_rejected(self):
        for grid in ([], [[]], [[1], [1, 2]], [[True]], [[1.5]],
                     [[10001]], [[-10001]], [['1']]):
            with self.assertRaises(ValueError):
                parse_grid({'cell_size_m': 1, 'elevations': grid})
        with self.assertRaises(ValueError):
            parse_grid({'cell_size_m': 0, 'elevations': [[1]]})
        with self.assertRaises(ValueError):
            parse_grid({'elevations': [[1]]})
        big = [[1] * 317 for _ in range(316)]  # 100172 cells
        with self.assertRaises(ValueError):
            parse_grid({'cell_size_m': 1, 'elevations': big})

    def test_invalid_queries_are_rejected(self):
        data = {'cell_size_m': 1, 'elevations': [[1, 2], [3, 4]]}
        elevations, h, w, area = parse_grid(data)
        bad_docs = [
            {}, {'queries': {}}, {'queries': [{}]},
            {'queries': [{'row': 0, 'col': 0}]},
            {'queries': [{'row': 0, 'col': 0, 'level': 0, 'x': 1}]},
            {'queries': [{'row': -1, 'col': 0, 'level': 0}]},
            {'queries': [{'row': 2, 'col': 0, 'level': 0}]},
            {'queries': [{'row': 0, 'col': 2, 'level': 0}]},
            {'queries': [{'row': 0, 'col': 0, 'level': 10001}]},
            {'queries': [{'row': 0, 'col': 0, 'level': -10001}]},
            {'queries': [{'row': True, 'col': 0, 'level': 0}]},
            {'queries': [{'row': 0, 'col': 0.5, 'level': 0}]},
            {'queries': [{'row': 0, 'col': 0, 'level': '0'}]},
        ]
        for doc in bad_docs:
            with self.assertRaises(ValueError):
                parse_queries(doc, h, w)
        with self.assertRaises(ValueError):
            parse_queries({'queries': [{'row': 0, 'col': 0, 'level': 0}]
                           * 100001}, h, w)

    def test_duplicate_json_keys_and_nonfinite_numbers_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            for raw in (b'{"queries": [], "queries": []}',
                        b'{"queries": [{"row": 0, "col": 0, "level": NaN}]}'):
                path = Path(td) / 'bad.json'
                path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    load_document(path)

    def test_query_order_is_preserved_and_independent(self):
        data = {'cell_size_m': 2, 'elevations': [[3, 1], [1, 3]]}
        queries = [{'row': 0, 'col': 1, 'level': 1},
                   {'row': 0, 'col': 0, 'level': 0},
                   {'row': 1, 'col': 0, 'level': 2},
                   {'row': 0, 'col': 0, 'level': 3}]
        result = self.build(data, queries)
        self.assertEqual([q is None for q in result['queries']],
                         [False, True, False, False])
        self.assertEqual(result['queries'][0]['area_cells'], 2)
        # At level 2 only the two level-1 cells are active; the level-3 cells
        # have not appeared yet.
        self.assertEqual(result['queries'][2]['area_cells'], 2)
        self.assertEqual(result['queries'][3]['area_cells'], 4)


class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.grid = self.tmp / 'grid.json'
        self.grid.write_text(json.dumps(
            {'cell_size_m': 2, 'elevations': [[1, 3, 1], [2, 5, 2], [1, 3, 1]]}))
        self.queries = self.tmp / 'queries.json'
        self.queries.write_text(json.dumps({'queries': [
            {'row': r, 'col': c, 'level': level}
            for level in (0, 1, 3, 5) for r in range(3) for c in range(3)]}))
        self.output = self.tmp / 'index.json'

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args):
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))
        return subprocess.run([sys.executable, '-m', 'terrain_flow.cli', *args],
                              capture_output=True, text=True, env=env)

    def test_connectivity_cli_writes_file_and_stdout(self):
        proc = self.run_cli('connectivity', '--grid', str(self.grid),
                            '--queries', str(self.queries),
                            '--output', str(self.output))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        on_disk = json.loads(self.output.read_text())
        streamed = json.loads(proc.stdout)
        self.assertEqual(on_disk, streamed)
        self.assertEqual(on_disk, run(self.grid, self.queries))
        self.assertEqual(len(on_disk['queries']), 36)

    def test_cli_failure_exits_2_and_preserves_output(self):
        proc_ok = self.run_cli('connectivity', '--grid', str(self.grid),
                               '--queries', str(self.queries),
                               '--output', str(self.output))
        self.assertEqual(proc_ok.returncode, 0)
        before = self.output.read_bytes()
        bad_queries = self.tmp / 'bad.json'
        bad_queries.write_text('{"queries": [{"row": 9, "col": 0, "level": 0}]}')
        proc = self.run_cli('connectivity', '--grid', str(self.grid),
                            '--queries', str(bad_queries),
                            '--output', str(self.output))
        self.assertEqual(proc.returncode, 2)
        self.assertIn('row', proc.stderr)
        self.assertEqual(self.output.read_bytes(), before)
        bad_grid = self.tmp / 'bad-grid.json'
        bad_grid.write_text('{}')
        proc = self.run_cli('connectivity', '--grid', str(bad_grid),
                            '--queries', str(self.queries),
                            '--output', str(self.output))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.output.read_bytes(), before)

    def test_output_collision_with_input_is_rejected(self):
        proc = self.run_cli('connectivity', '--grid', str(self.grid),
                            '--queries', str(self.queries),
                            '--output', str(self.grid))
        self.assertEqual(proc.returncode, 2)
        self.assertIn('differ', proc.stderr)

    def test_existing_commands_still_work_alongside_connectivity(self):
        report = self.tmp / 'report.json'
        proc = self.run_cli('--source', str(ROOT / 'fixtures/terrain.json'),
                            '--output', str(report))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('catchments', json.loads(report.read_text()))
        proc = self.run_cli('connectivity', '--help')
        self.assertEqual(proc.returncode, 0)
        self.assertIn('merge-forest', proc.stdout)

    def test_shipped_fixture_pair_runs_and_matches_library_call(self):
        output = self.tmp / 'fixture-index.json'
        proc = self.run_cli(
            'connectivity',
            '--grid', str(ROOT / 'fixtures/terrain.json'),
            '--queries', str(ROOT / 'fixtures/terrain-queries.json'),
            '--output', str(output))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        on_disk = json.loads(output.read_text())
        self.assertEqual(on_disk, run(ROOT / 'fixtures/terrain.json',
                                      ROOT / 'fixtures/terrain-queries.json'))
        self.assertEqual(on_disk['shape'], [3, 5])
        self.assertEqual(on_disk['node_count'], 2)
        # The last two fixture queries hit a below-activation level and a null.
        self.assertIsNone(on_disk['queries'][-1])
        self.assertIsNone(on_disk['queries'][-2])
        self.assertTrue(all(isinstance(q, int)
                            for node in on_disk['forest']
                            for q in node['children']))


if __name__ == '__main__':
    unittest.main()
