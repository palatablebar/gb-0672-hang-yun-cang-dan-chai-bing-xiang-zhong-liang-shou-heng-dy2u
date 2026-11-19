from fastapi.testclient import TestClient
from app import main, store
from app.config import ROOT

CLEAN_RECEIPT = (ROOT / 'sample_data/receipt_clean.csv').read_text()
DIRTY_RECEIPT = (ROOT / 'sample_data/receipt_dirty.csv').read_text()


def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'test.sqlite'))
    return TestClient(main.app)


def test_confirm_success_and_current_persisted(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={'dataset': 'clean'}).json()
    rid = run['manifest']['run_id']
    resp = c.post(f'/api/runs/{rid}/booking/confirm',
                  json={'receipt_text': CLEAN_RECEIPT, 'release_mode': 'proven'})
    assert resp.status_code == 200
    data = resp.json()
    assert data['status'] == '现行' and data['reused'] is False
    assert data['booking']['summary']['accepted'] == 6
    got = c.get(f'/api/runs/{rid}/booking').json()
    assert got['current']['seal'] == data['seal']
    assert len(got['versions']) == 1


def test_rerun_same_receipt_no_second_set(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={'dataset': 'clean'}).json()
    rid = run['manifest']['run_id']
    url = f'/api/runs/{rid}/booking/confirm'
    first = c.post(url, json={'receipt_text': CLEAN_RECEIPT}).json()
    second = c.post(url, json={'receipt_text': CLEAN_RECEIPT}).json()
    assert first['id'] == second['id']
    assert second['reused'] is True
    versions = c.get(f'/api/runs/{rid}/booking').json()['versions']
    assert len(versions) == 1  # 同一回执不新增第二套


def test_failed_confirmation_keeps_previous_version(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={'dataset': 'clean'}).json()
    rid = run['manifest']['run_id']
    url = f'/api/runs/{rid}/booking/confirm'
    good = c.post(url, json={'receipt_text': CLEAN_RECEIPT}).json()
    # 全部箱都无法进入确认单：构造来源重复的整票回执
    all_dup = 'container,pieces,gross_kg\n' + '\n'.join(
        f'{b},40,440\n{b},41,440' for b in ('C001', 'C002', 'C003', 'C004', 'C005', 'C006'))
    fail = c.post(url, json={'receipt_text': all_dup})
    assert fail.status_code == 409
    assert fail.json()['code'] == 'CONFIRMATION_FAILED'
    current = c.get(f'/api/runs/{rid}/booking').json()
    assert current['current']['seal'] == good['seal']  # 旧版确认单保留
    assert len(current['versions']) == 1


def test_new_distinct_receipt_supersedes_old_but_keeps_history(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={'dataset': 'clean'}).json()
    rid = run['manifest']['run_id']
    url = f'/api/runs/{rid}/booking/confirm'
    first = c.post(url, json={'receipt_text': CLEAN_RECEIPT}).json()
    # 新回执：C001 毛重微调，其余对平，仍是可确认的新裁决
    new_receipt = CLEAN_RECEIPT.replace('C001,40,440', 'C001,40,440')
    new_receipt = 'container,pieces,gross_kg\nC001,40,440\nC002,40,440\nC003,40,440\nC004,40,440\nC005,40,440\nC006,40,439\n'
    second = c.post(url, json={'receipt_text': new_receipt}).json()
    assert second['id'] != first['id']
    versions = c.get(f'/api/runs/{rid}/booking').json()['versions']
    assert {v['status'] for v in versions} == {'现行', '历史'}
    assert versions[0]['status'] == '现行'


def test_partial_release_in_proven_mode_persists_provable_boxes(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={'dataset': 'clean'}).json()
    rid = run['manifest']['run_id']
    data = c.post(f'/api/runs/{rid}/booking/confirm',
                  json={'receipt_text': DIRTY_RECEIPT, 'release_mode': 'proven'}).json()
    s = data['booking']['summary']
    assert s['accepted'] == 2 and s['decision'] == '部分放行可证明箱'
    # 退回/待补证箱不进确认计量
    assert s['confirmed_pieces'] == 80


def test_whole_mode_rejects_partial_confirmation(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={'dataset': 'clean'}).json()
    rid = run['manifest']['run_id']
    # whole 模式下仍有已接受箱（2个），确认单可落但整票不交接
    data = c.post(f'/api/runs/{rid}/booking/confirm',
                  json={'receipt_text': DIRTY_RECEIPT, 'release_mode': 'whole'}).json()
    assert data['booking']['summary']['released'] is False
    assert data['booking']['summary']['whole_release_ok'] is False


def test_booking_page_and_api_entry(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={'dataset': 'clean'}).json()
    rid = run['manifest']['run_id']
    assert c.get(f'/booking?run_id={rid}').status_code == 200
    assert c.post('/api/booking', json={'dataset': 'clean', 'receipt_text': CLEAN_RECEIPT}).status_code == 200


def test_bad_release_mode_rejected(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={'dataset': 'clean'}).json()
    rid = run['manifest']['run_id']
    resp = c.post(f'/api/runs/{rid}/booking/confirm',
                  json={'receipt_text': CLEAN_RECEIPT, 'release_mode': 'rush'})
    assert resp.status_code == 422
