import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fds.backend import JeffBackend

class CapabilitiesTest(unittest.TestCase):
    def test_installed_and_devices(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);folder=root/'models/ready';folder.mkdir(parents=True)
            (folder/'decision_config.json').write_text('{}');(folder/'.fds-revision').write_text('fixed');(folder/'model.safetensors').touch()
            config={'default_device':'auto','models':{key:{'name':key,'revision':'fixed','checkpoint':'models/'+key,'backend':'jeff','images':True} for key in ['ready','missing']}}
            backend=JeffBackend(root,config)
            with patch('torch.cuda.is_available',return_value=False):
                result=backend.capabilities()
                self.assertEqual(result['devices'],['auto','cpu'])
                self.assertTrue(result['models'][0]['available'])
                self.assertFalse(result['models'][1]['available'])
                self.assertEqual(result['models'][1]['devices'],[])
            from types import SimpleNamespace
            with patch('torch.cuda.is_available',return_value=False):
                with self.assertRaisesRegex(ValueError, "指定デバイス"):
                    backend.validate_request(SimpleNamespace(model="ready", device="cuda"))
                with self.assertRaisesRegex(ValueError, "未導入"):
                    backend.validate_request(SimpleNamespace(model="missing", device="cpu"))
                backend.validate_request(SimpleNamespace(model="ready", device="cpu"))
            with patch('torch.cuda.is_available',return_value=True):
                self.assertIn('cuda',backend.capabilities()['models'][0]['devices'])
            (folder/'.fds-revision').write_text('old')
            self.assertFalse(backend.capabilities()['models'][0]['available'])

if __name__=='__main__':unittest.main()
