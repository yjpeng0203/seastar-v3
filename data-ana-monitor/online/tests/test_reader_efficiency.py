from pathlib import Path
import sys,tempfile,unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from acquisition import DatReader,SourceChanged,discover

class ReaderEfficiencyTest(unittest.TestCase):
 def test_184_sealed_volumes_idle_checks_and_old_change(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t)
   for i in range(1,185):(root/f'data_fee_4_run000001_idx{i:03}_20261111000000.dat').write_bytes(b'x'*1024)
   source=discover(root)[0];reader=DatReader(root)
   while reader.read(source):pass
   original=Path.open;calls=[]
   def counted(p,*a,**k):calls.append(p);return original(p,*a,**k)
   with patch.object(Path,'open',counted):
    for _ in range(5):self.assertEqual(reader.read(source),b'')
   self.assertEqual(calls,[])
   restart=DatReader(root,reader.state())
   with patch.object(Path,'open',counted):self.assertEqual(restart.read(source),b'')
   self.assertEqual(len(calls),184)
   old=root/source['files'][0]['name']
   with old.open('r+b') as f:f.seek(500);f.write(b'y')
   with self.assertRaises(SourceChanged):reader.read(source)

if __name__=='__main__':unittest.main()
