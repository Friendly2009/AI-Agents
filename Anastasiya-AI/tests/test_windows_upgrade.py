import importlib.util, sys, types, os, unittest
from pathlib import Path
from unittest.mock import patch, Mock
p=Path(__file__).resolve().parents[1] / 'desktop_release' / 'windows_service.py'
with patch.dict(sys.modules,winreg=types.ModuleType('winreg')),patch.dict(os.environ,SystemRoot='/windows'):
 spec=importlib.util.spec_from_file_location('win',p);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
old=m.HOST.parent/'old'
class TestUpgrade(unittest.TestCase):
 def run_case(self,states,paths):
  with patch.object(m,'status',side_effect=states),patch.object(m,'check_owner',side_effect=paths),patch.object(m,'command') as cmd:
   m.upgrade_existing()
   return [x.args[0] for x in cmd.call_args_list]
 def test_running(self): self.assertEqual(self.run_case([4,1,4],[old,m.HOST]),['stop','config','start'])
 def test_stopped(self): self.assertEqual(self.run_case([1],[old,m.HOST]),['config'])
 def test_new(self): self.assertEqual(self.run_case([0],[]),[])
 def test_same(self): self.assertEqual(self.run_case([4],[m.HOST]),[])
 def test_rejected(self):
  with patch.object(m,'status',return_value=4),patch.object(m,'check_owner',side_effect=ValueError),patch.object(m,'command') as cmd:
   with self.assertRaises(ValueError):m.upgrade_existing()
   cmd.assert_not_called()
 def test_rollback(self):
  with patch.object(m,'status',side_effect=[4,1,1]),patch.object(m,'check_owner',side_effect=[old,m.HOST]),patch.object(m,'command',side_effect=[None,None,OSError,None,None]) as cmd:
   with self.assertRaises(OSError):m.upgrade_existing()
   self.assertIn(str(old),cmd.call_args_list[-2].args[-1])
