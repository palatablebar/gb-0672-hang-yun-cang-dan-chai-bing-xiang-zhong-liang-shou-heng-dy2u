from collections import defaultdict
from decimal import Decimal
import csv, hashlib, io
from app.coerce import number

# 箱状态
ACCEPTED = '已接受'        # 重量对平、来源唯一且覆盖完整，允许进入确认单
PENDING = '待补证'         # 箱内明细暂不能覆盖回执整箱件重，需补证后重判
RETURNED = '退回'          # 重量未对平或来源重复，禁止进入确认单

# 原因码
RECEIPT_DUP = 'RECEIPT_DUPLICATE'          # 同一箱号在回执中重复且值不一致
RECEIPT_WEIGHT_MISMATCH = 'WEIGHT_MISMATCH'  # 箱内主单合计与回执整箱件重对不平
UNKNOWN_CONTAINER = 'UNKNOWN_CONTAINER'    # 回执箱号在舱单中无归属
MULTI_SOURCE = 'MULTI_SOURCE'              # 箱归属多张主单，来源需逐主单拆清
UNCOVERED = 'UNCOVERED_CARGO'              # 箱内明细未能覆盖回执报告的整箱量
NO_RECEIPT = 'NO_RECEIPT'                  # 回执未报告该箱

ZERO = Decimal('0')


def parse_receipts(raw_text):
    """解析船公司回执 CSV。回执只报整箱件重：container,pieces,gross_kg。

    同箱重复且整箱件重一致 -> 折叠为一条并计数；不一致 -> 全部保留并打 RECEIPT_DUPLICATE。
    返回 (rows, duplicates)。rows 按箱号排序，保证结果顺序稳定。
    """
    if not str(raw_text or '').strip():
        return [], []
    stream = csv.reader(io.StringIO(str(raw_text), newline=''), strict=True)
    header = next(stream)
    header = [h.strip().lstrip('﻿') for h in header]
    if header != ['container', 'pieces', 'gross_kg']:
        raise ValueError('回执表头须为 container,pieces,gross_kg')
    grouped = defaultdict(list)
    for cells in stream:
        if not cells or not any(c.strip() for c in cells):
            continue
        if len(cells) != 3:
            raise ValueError('回执行列数错误')
        container = cells[0].strip()
        grouped[container].append((int(number(cells[1])), str(number(cells[2]).quantize(Decimal('0.001')))))
    rows, duplicates = [], []
    for container in sorted(grouped):
        entries = grouped[container]
        distinct = set(entries)
        if len(distinct) > 1:
            duplicates.append({'container': container, 'code': RECEIPT_DUP,
                               'detail': f'同箱回执{len(entries)}条且件重不一致', 'entries': entries})
            # 来源重复的箱不采信任何一条
            continue
        pieces, gross = entries[0]
        rows.append({'container': container, 'pieces': pieces,
                     'gross_kg': gross, 'receipt_count': len(entries)})
    return rows, duplicates


def build_booking(state, receipt_text=None, release_mode='proven'):
    """依据舱单采纳行与船公司回执，逐箱裁决 已接受/待补证/退回。

    release_mode:
      proven       先放行可证明的箱（默认，赶截单）；
      whole        整票齐后交接，任一箱不达标则整票不交接。
    只按主单（根节点）汇总，子单/分单不重复计量。
    """
    if release_mode not in ('proven', 'whole'):
        raise ValueError('release_mode 仅支持 proven / whole')
    records = state['records']
    roots = set(state['graph']['roots'])

    # 箱 -> 该箱内出现的主单；主单级行携带整棵子树的件重，天然不重复计量
    box_root_rows = defaultdict(list)
    for r in records:
        if r['id'] in roots:
            box_root_rows[r['container']].append(r)

    receipt_rows, receipt_duplicates = parse_receipts(
        receipt_text if receipt_text is not None else state['context'].get('carrier_receipt', ''))
    receipt_by_box = {r['container']: r for r in receipt_rows}
    dup_boxes = {d['container'] for d in receipt_duplicates}

    all_boxes = sorted(set(box_root_rows) | set(receipt_by_box) | dup_boxes)
    containers, allocations = [], []

    for box in all_boxes:
        root_rows = sorted(box_root_rows.get(box, []), key=lambda r: r['id'])
        manifest_pieces = sum(r['pieces'] for r in root_rows)
        manifest_gross = sum((Decimal(r['gross_kg']) for r in root_rows), ZERO)
        receipt = receipt_by_box.get(box)
        codes, detail_parts = [], []

        if box in dup_boxes:
            codes.append(RECEIPT_DUP)
            detail_parts.append('回执同箱来源重复，按退回处理且不采信任一条')
        if not root_rows:
            codes.append(UNKNOWN_CONTAINER)
            detail_parts.append('回执箱号在舱单中无任何主单归属')
        if len({r['id'] for r in root_rows}) > 1:
            codes.append(MULTI_SOURCE)
            detail_parts.append(f"箱内挂{len(root_rows)}张主单：{','.join(r['id'] for r in root_rows)}")

        # 重量对平：舱单主单合计 vs 回执整箱件重
        balanced = False
        if receipt is not None and root_rows and box not in dup_boxes:
            pieces_ok = manifest_pieces == receipt['pieces']
            gross_ok = manifest_gross == Decimal(receipt['gross_kg'])
            balanced = pieces_ok and gross_ok
            if not pieces_ok:
                codes.append(RECEIPT_WEIGHT_MISMATCH)
                detail_parts.append(f"件数 舱单{manifest_pieces}/回执{receipt['pieces']}")
            if not gross_ok:
                codes.append(RECEIPT_WEIGHT_MISMATCH)
                detail_parts.append(f"毛重kg 舱单{manifest_gross}/回执{receipt['gross_kg']}")
        elif receipt is None and box not in dup_boxes:
            codes.append(NO_RECEIPT)
            detail_parts.append('船公司回执未报告该箱')

        # 覆盖完整性：有归属主单但对不平，视为舱单明细未覆盖回执整箱量
        if root_rows and receipt is not None and not balanced and box not in dup_boxes:
            codes.append(UNCOVERED)

        if RECEIPT_DUP in codes or UNKNOWN_CONTAINER in codes:
            status = RETURNED
        elif balanced and MULTI_SOURCE not in codes:
            status = ACCEPTED
        else:
            # 重量未对平、回执缺失或多主单归属不清：保留原明细，待补证后重判
            if MULTI_SOURCE in codes:
                detail_parts.append('归属不清，确认单不覆盖，保留各主单原明细待人工拆清')
            status = PENDING

        containers.append({
            'container': box,
            'status': status,
            'codes': sorted(set(codes)),
            'detail': '；'.join(detail_parts) or '件重对平、来源唯一',
            'manifest_pieces': manifest_pieces,
            'manifest_gross_kg': str(manifest_gross),
            'receipt_pieces': receipt['pieces'] if receipt else None,
            'receipt_gross_kg': receipt['gross_kg'] if receipt else None,
            'master_bills': [r['id'] for r in root_rows],
        })
        # 确认后的箱内明细回到各主单：按主单原样回写，不做二次加总
        for r in root_rows:
            allocations.append({
                'container': box, 'master_bill': r['id'], 'pieces': r['pieces'],
                'net_kg': r['net_kg'], 'gross_kg': r['gross_kg'],
                'box_status': status, 'counted_in_confirmation': status == ACCEPTED,
            })

    accepted = [c for c in containers if c['status'] == ACCEPTED]
    pending = [c for c in containers if c['status'] == PENDING]
    returned = [c for c in containers if c['status'] == RETURNED]

    # 整票交接：每个箱都必须说得清来源，全部已接受才交接
    whole_release_ok = len(containers) > 0 and not pending and not returned
    if release_mode == 'whole':
        released = whole_release_ok
    else:
        released = bool(accepted)  # proven：先按可证明的箱量放行

    # 确认单计量口径：只统计已接受箱，且按主单一次计量
    confirmed_pieces = sum(c['manifest_pieces'] for c in accepted)
    confirmed_gross = sum((Decimal(c['manifest_gross_kg']) for c in accepted), ZERO)

    evidence = {'containers': containers, 'receipt_duplicates': receipt_duplicates,
                'release_mode': release_mode}
    seal = hashlib.sha256(
        (receipt_text or state['context'].get('carrier_receipt', ''))
        .encode() + hashlib.sha256(
            str(sorted((c['container'], c['status'], tuple(c['codes'])) for c in containers)).encode()
        ).digest()
    ).hexdigest()

    return {
        'release_mode': release_mode,
        'containers': containers,
        'allocations': allocations,
        'receipt_duplicates': receipt_duplicates,
        'summary': {
            'total_boxes': len(containers), 'accepted': len(accepted),
            'pending': len(pending), 'returned': len(returned),
            'confirmed_pieces': confirmed_pieces,
            'confirmed_gross_kg': str(confirmed_gross),
            'released': released,
            'whole_release_ok': whole_release_ok,
            'decision': ('整票交接' if whole_release_ok else
                         ('部分放行可证明箱' if released and release_mode == 'proven' else '不予交接')),
        },
        'seal': seal,
    }


def apply(state):
    state['booking'] = build_booking(state)
    state['views']['booking'] = {
        'title': '订舱确认单',
        'rows': state['booking']['containers'],
        'metrics': {
            '已接受': state['booking']['summary']['accepted'],
            '待补证': state['booking']['summary']['pending'],
            '退回': state['booking']['summary']['returned'],
            '交接裁决': state['booking']['summary']['decision'],
        },
    }
