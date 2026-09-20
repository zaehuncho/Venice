"""Export publication contract. Uses no training, torch, ONNX, or real checkpoint."""
import importlib.util
import os
from pathlib import Path
import sys
import types

import pytest


@pytest.fixture
def exporter(tmp_path, monkeypatch):
    default_source=next(parent/'tools'/'training'/'finetune_meter_lowfill.py'
                        for parent in Path(__file__).resolve().parents
                        if (parent/'tools'/'training'/'finetune_meter_lowfill.py').is_file())
    source=Path(os.environ.get('FINETUNE_MODULE',str(default_source))).resolve()
    spec=importlib.util.spec_from_file_location('finetune_export_under_test',source)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.PROJECT=str(tmp_path)
    weights=tmp_path/'meter2k27_n4_test'/'weights'
    weights.mkdir(parents=True)
    (weights/'best.pt').write_bytes(b'mock checkpoint remains unchanged')
    control={'fail_size':None,'exports':[]}
    class FakeYOLO:
        def __init__(self,path): self.path=Path(path)
        def export(self,**kwargs):
            assert kwargs['format']=='onnx' and kwargs['quantize']==16
            assert kwargs['batch']==1 and kwargs['dynamic'] is False
            sz=kwargs['imgsz']
            control['exports'].append(sz)
            path=self.path.with_suffix('.onnx')
            path.write_bytes(f'mock onnx resolution={sz}'.encode())
            if sz==control['fail_size']:
                raise RuntimeError('injected later export failure')
            return str(path)
    monkeypatch.setitem(sys.modules,'ultralytics',types.SimpleNamespace(YOLO=FakeYOLO))
    def run(sizes=(960,1280)):
        monkeypatch.setattr(sys,'argv',[str(source),'--skip-train','--name','meter2k27_n4_test',
                                       '--export-imgsz',*map(str,sizes)])
        module.main()
    return module,weights,control,run


@pytest.mark.parametrize('sizes',[(960,1280),(1280,960)])
def test_both_resolutions_survive_in_correct_paths(exporter,sizes):
    module,weights,control,run=exporter
    run(sizes)
    assert (weights/'best.onnx').read_bytes()==b'mock onnx resolution=960'
    assert (weights/'best_1280.onnx').read_bytes()==b'mock onnx resolution=1280'
    assert (weights/'best.pt').read_bytes()==b'mock checkpoint remains unchanged'
    assert not list(weights.glob('.lowfill-export-*'))


def test_later_export_failure_keeps_existing_exports(exporter):
    module,weights,control,run=exporter
    (weights/'best.onnx').write_bytes(b'previous 960')
    (weights/'best_1280.onnx').write_bytes(b'previous 1280')
    control['fail_size']=1280
    with pytest.raises(RuntimeError,match='later export failure'): run()
    assert (weights/'best.onnx').read_bytes()==b'previous 960'
    assert (weights/'best_1280.onnx').read_bytes()==b'previous 1280'


def test_later_export_failure_publishes_nothing_when_no_previous(exporter):
    module,weights,control,run=exporter
    control['fail_size']=1280
    with pytest.raises(RuntimeError,match='later export failure'): run()
    assert not list(weights.glob('*.onnx'))


def test_second_publication_failure_restores_previous_exports(exporter,monkeypatch):
    module,weights,control,run=exporter
    (weights/'best.onnx').write_bytes(b'previous 960')
    (weights/'best_1280.onnx').write_bytes(b'previous 1280')
    original_replace=module.os.replace
    def fail_second(src,dst):
        if Path(dst).name=='best_1280.onnx':
            raise PermissionError('injected locked destination')
        return original_replace(src,dst)
    monkeypatch.setattr(module.os,'replace',fail_second)
    with pytest.raises(PermissionError,match='locked destination'): run()
    assert (weights/'best.onnx').read_bytes()==b'previous 960'
    assert (weights/'best_1280.onnx').read_bytes()==b'previous 1280'


def test_failed_rollback_retains_backup_and_original_error(exporter,monkeypatch):
    module,weights,control,run=exporter
    (weights/'best.onnx').write_bytes(b'previous 960')
    (weights/'best_1280.onnx').write_bytes(b'previous 1280')
    original_replace=module.os.replace
    def fail_publish_and_restore(src,dst):
        if Path(dst).name=='best_1280.onnx':
            raise PermissionError('injected publication failure')
        if Path(src).name=='best.onnx.previous':
            raise PermissionError('injected restoration failure')
        return original_replace(src,dst)
    monkeypatch.setattr(module.os,'replace',fail_publish_and_restore)
    with pytest.raises(RuntimeError,match='retained recovery directory') as error: run()
    assert isinstance(error.value.__cause__,PermissionError)
    assert str(error.value.__cause__)=='injected publication failure'
    retained=list(weights.glob('.lowfill-export-*'))
    assert len(retained)==1
    assert str(retained[0]) in str(error.value)
    assert (retained[0]/'best.onnx.previous').read_bytes()==b'previous 960'
    assert (retained[0]/'best_1280.onnx.previous').read_bytes()==b'previous 1280'
    assert (weights/'best_1280.onnx').read_bytes()==b'previous 1280'
