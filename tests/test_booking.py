from decimal import Decimal
from copy import deepcopy
from app.engine import evaluate
from app.modules.booking.rules import build_booking, parse_receipts, ACCEPTED, PENDING, RETURNED
from app.config import ROOT

CLEAN_RECEIPT = (ROOT / 'sample_data/receipt_clean.csv').read_text()
DIRTY_RECEIPT = (ROOT / 'sample_data/receipt_dirty.csv').read_text()


def status_of(booking, box):
    return next(c for c in booking['containers'] if c['container'] == box)


def test_clean_receipt_all_accepted(clean):
    booking = build_booking(clean, CLEAN_RECEIPT)
    assert len(booking['containers']) == 6
    assert {c['status'] for c in booking['containers']} == {ACCEPTED}
    assert booking['summary']['confirmed_pieces'] == 240
    assert booking['summary']['confirmed_gross_kg'] == '2640'
    assert booking['summary']['released'] is True
    assert booking['summary']['whole_release_ok'] is True


def test_weight_mismatch_becomes_pending(clean):
    # C002 件数对不平、C003 毛重对不平
    booking = build_booking(clean, DIRTY_RECEIPT)
    c2 = status_of(booking, 'C002')
    assert c2['status'] == PENDING and 'WEIGHT_MISMATCH' in c2['codes'] and 'UNCOVERED_CARGO' in c2['codes']
    c3 = status_of(booking, 'C003')
    assert c3['status'] == PENDING and 'WEIGHT_MISMATCH' in c3['codes']


def test_receipt_duplicate_is_returned_and_not_trusted(clean):
    booking = build_booking(clean, DIRTY_RECEIPT)
    c4 = status_of(booking, 'C004')
    assert c4['status'] == RETURNED and 'RECEIPT_DUPLICATE' in c4['codes']
    assert any(d['container'] == 'C004' for d in booking['receipt_duplicates'])
    # 重复回执的任何一条都不得被采信
    assert c4['receipt_pieces'] is None


def test_unknown_container_returned(clean):
    booking = build_booking(clean, DIRTY_RECEIPT)
    c999 = status_of(booking, 'C999')
    assert c999['status'] == RETURNED and 'UNKNOWN_CONTAINER' in c999['codes']


def test_missing_receipt_pending_not_accepted(clean):
    # 只报 4 个箱，C005/C006 回执缺失
    text = 'container,pieces,gross_kg\nC001,40,440\nC002,40,440\nC003,40,440\nC004,40,440\n'
    booking = build_booking(clean, text)
    assert status_of(booking, 'C005')['status'] == PENDING
    assert 'NO_RECEIPT' in status_of(booking, 'C005')['codes']
    assert booking['summary']['accepted'] == 4


def test_proven_mode_releases_only_provable_boxes(clean):
    booking = build_booking(clean, DIRTY_RECEIPT, release_mode='proven')
    # 可证明的箱（C001/C005 对平）先放行，问题箱不进入确认单
    assert booking['summary']['released'] is True
    assert booking['summary']['decision'] == '部分放行可证明箱'
    accepted_boxes = {c['container'] for c in booking['containers'] if c['status'] == ACCEPTED}
    assert accepted_boxes == {'C001', 'C005'}
    assert booking['summary']['confirmed_pieces'] == 80


def test_whole_mode_blocks_handover_until_complete(clean):
    booking = build_booking(clean, DIRTY_RECEIPT, release_mode='whole')
    assert booking['summary']['whole_release_ok'] is False
    assert booking['summary']['released'] is False
    assert booking['summary']['decision'] == '不予交接'
    # 整票齐后（全部对平）才交接
    whole = build_booking(clean, CLEAN_RECEIPT, release_mode='whole')
    assert whole['summary']['released'] is True
    assert whole['summary']['decision'] == '整票交接'


def test_allocations_back_to_master_bills_no_double_count(clean):
    booking = build_booking(clean, CLEAN_RECEIPT)
    # 每个主单在其箱内只出现一次，件重取主单根值（不叠加子单）
    per_master = {}
    for a in booking['allocations']:
        per_master.setdefault(a['master_bill'], []).append(a)
    assert len(per_master) == 6
    for bill, rows in per_master.items():
        assert len(rows) == 1
        assert rows[0]['counted_in_confirmation'] is True
    assert sum(a['pieces'] for a in booking['allocations'] if a['counted_in_confirmation']) == 240


def test_pending_box_allocations_not_counted(clean):
    booking = build_booking(clean, DIRTY_RECEIPT)
    pending_boxes = {c['container'] for c in booking['containers'] if c['status'] == PENDING}
    for a in booking['allocations']:
        if a['container'] in pending_boxes:
            assert a['counted_in_confirmation'] is False
    # 已接受箱的明细仍回写主单，确认合计只含已接受箱
    counted = sum(a['pieces'] for a in booking['allocations'] if a['counted_in_confirmation'])
    assert counted == booking['summary']['confirmed_pieces'] == 80


def test_multi_master_shared_container_flagged():
    # 两张主单同箱：来源必须拆清，箱不得直接按整箱接受
    csv_text = '\n'.join([
        'id,parent,packages,package_unit,net,gross,weight_unit,hazard,un_code,container,revision',
        'MA,,20,carton,200,220,kg,none,,SHARE,1',
        'MB,,20,carton,200,220,kg,none,,SHARE,1',
    ])
    state = evaluate({'dataset': 'clean', 'csv_text': csv_text})
    booking = build_booking(state, 'container,pieces,gross_kg\nSHARE,40,440\n')
    box = status_of(booking, 'SHARE')
    assert 'MULTI_SOURCE' in box['codes']
    # 重量虽对平，但归属不清时确认单不覆盖：保留两张主单各自明细，待补证拆清
    assert box['status'] == PENDING
    assert sorted(box['master_bills']) == ['MA', 'MB']
    assert len(booking['allocations']) == 2  # 原明细全部保留，不合并丢失


def test_parse_receipt_folds_identical_duplicates():
    rows, dups = parse_receipts('container,pieces,gross_kg\nC1,40,440\nC1,40,440\n')
    assert dups == []
    assert rows == [{'container': 'C1', 'pieces': 40, 'gross_kg': '440.000', 'receipt_count': 2}]


def test_parse_receipt_flags_conflicting_duplicates():
    rows, dups = parse_receipts('container,pieces,gross_kg\nC1,40,440\nC1,41,440\n')
    assert rows == []
    assert dups[0]['code'] == 'RECEIPT_DUPLICATE'


def test_order_invariant(clean):
    a = build_booking(clean, DIRTY_RECEIPT)
    lines = DIRTY_RECEIPT.splitlines()
    shuffled = '\n'.join([lines[0]] + list(reversed(lines[1:])))
    b = build_booking(clean, shuffled)
    assert [(c['container'], c['status'], tuple(c['codes'])) for c in a['containers']] == \
           [(c['container'], c['status'], tuple(c['codes'])) for c in b['containers']]
