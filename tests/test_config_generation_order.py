from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bot'))
import config_store as cs


class GenerationOrderingTests(unittest.TestCase):
    def test_rollback_order_is_chronological_across_single_and_double_digit_milliseconds(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'config.env'
            p.write_text('X=0\n',encoding='utf-8')
            with patch.object(cs.time,'time',return_value=1794058000.002):
                cs.set_values({'X':'1'},str(p))
            with patch.object(cs.time,'time',return_value=1794058000.012):
                cs.set_values({'X':'2'},str(p))
            with patch.object(cs.time,'time',return_value=1794058000.032):
                cs.restore(str(p),which=-1)
            self.assertEqual(cs.read_values(str(p))['X'],'1')


if __name__=='__main__':unittest.main()
