from fastapi import APIRouter, Request, Body
from app.engine import evaluate
from app.web import render
from app import store
from app.errors import BusinessError
from app.modules.booking.rules import build_booking

router = APIRouter()


def _db():
    # 延迟读取，与 app.web 一致，允许测试 monkeypatch app.main.DB_PATH
    from app.main import DB_PATH
    return DB_PATH


@router.get('/booking')
def page(request: Request):
    return render(request, 'booking')


@router.post('/api/booking')
def calculate(payload: dict = Body(default={})):
    """只演算订舱确认单，不落库确认；可带 receipt_text 与 release_mode。"""
    state = evaluate({'dataset': payload.get('dataset', 'dirty'),
                      'csv_text': payload['csv_text']} if 'csv_text' in payload
                     else {'dataset': payload.get('dataset', 'dirty')})
    state['booking'] = build_booking(state, payload.get('receipt_text'),
                                     payload.get('release_mode', 'proven'))
    return state


@router.post('/api/runs/{run_id}/booking/confirm')
def confirm(run_id: str, payload: dict = Body(...)):
    """生成订舱确认单。

    - 重量未对平或来源重复的箱不得进入确认单；
    - 确认失败保留旧版确认单；
    - 同一回执重跑不新增第二套（按回执+裁决结果幂等）。
    """
    db_path = _db()
    previous = store.get(db_path, run_id)
    receipt_text = payload.get('receipt_text')
    release_mode = payload.get('release_mode', 'proven')
    if release_mode not in ('proven', 'whole'):
        raise BusinessError('BAD_RELEASE_MODE', '放行方式仅支持 proven / whole')
    booking = build_booking(previous, receipt_text, release_mode)
    return store.confirm_booking(db_path, run_id, booking, receipt_text or '')
