import json, unittest, tempfile, subprocess, sys
from pathlib import Path
from terrain_flow.drainage import drain
ROOT=Path(__file__).resolve().parents[1]
class DrainageTests(unittest.TestCase):
    def setUp(self):self.data=json.loads((ROOT/'fixtures/terrain.json').read_text())
    def test_known_catchments_and_area_conservation(self):
        r=drain(self.data);self.assertEqual(r['total_area_m2'],48)
        self.assertEqual(r['catchments'],[{'outlet':[2,2],'cells':9,'area_m2':36},{'outlet':[2,4],'cells':3,'area_m2':12}])
        self.assertEqual(sum(c['area_m2'] for c in r['catchments']),r['total_area_m2'])
    def test_slope_not_lowest_elevation(self):
        r=drain({'cell_size_m':1,'elevations':[[10,7],[8,6]]})
        self.assertEqual(r['cells'][0]['downstream'],[0,1])
    def test_tied_slope_chooses_lexicographic_cell(self):
        r=drain({'cell_size_m':1,'elevations':[[2,3,2]]})
        self.assertEqual(r['cells'][1]['downstream'],[0,0])
    def test_plateau_is_not_artificially_drained(self):
        r=drain({'cell_size_m':1,'elevations':[[5,5],[5,5]]})
        self.assertEqual(len(r['catchments']),4);self.assertTrue(all(c['upstream_cells']==1 for c in r['cells']))
    def test_nodata_and_negative_elevation(self):
        r=drain({'cell_size_m':1,'elevations':[[None,-2],[-3,None]]})
        self.assertEqual(r['catchments'][0]['outlet'],[1,0]);self.assertEqual(r['valid_cells'],2)
        self.assertEqual(drain({'cell_size_m':1,'elevations':[[None]]})['catchments'],[])
    def test_invalid_grid_and_boolean_elevation(self):
        for grid in ([],[[1],[1,2]],[[True]],[[1.5]]):
            with self.assertRaises(ValueError):drain({'cell_size_m':1,'elevations':grid})
    def test_every_edge_descends_and_accumulation_matches_walks(self):
        grid=[[8,4,9],[7,2,6],[1,5,3]];r=drain({'cell_size_m':3,'elevations':grid})
        cells={(c['row'],c['col']):c for c in r['cells']};counts={p:0 for p in cells}
        for p in cells:
            visited=set()
            while p is not None:
                self.assertNotIn(p,visited);visited.add(p);counts[p]+=1;c=cells[p];n=c['downstream']
                if n is not None:self.assertLess(cells[tuple(n)]['elevation_m'],c['elevation_m'])
                p=tuple(n) if n is not None else None
        self.assertEqual(counts,{p:c['upstream_cells'] for p,c in cells.items()})
    def test_installed_cli_and_error_preserves_output(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'report.json';args=[sys.executable,'-m','terrain_flow.cli','--output',str(out),'--source']
            r=subprocess.run(args+[str(ROOT/'fixtures/terrain.json')],capture_output=True)
            self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(json.loads(out.read_text()),drain(self.data))
            before=out.read_bytes();bad=Path(td)/'bad.json';bad.write_text('{}')
            self.assertEqual(subprocess.run(args+[str(bad)],capture_output=True).returncode,2);self.assertEqual(out.read_bytes(),before)
if __name__=='__main__':unittest.main()
