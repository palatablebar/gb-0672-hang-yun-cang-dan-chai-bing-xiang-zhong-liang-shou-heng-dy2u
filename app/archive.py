import io, csv, json, zipfile, hashlib
from app.errors import BusinessError

def serialize(result):
    if not result.get('manifest') or not result.get('views'):
        raise BusinessError('INCOMPLETE_RUN','批次尚未完成')
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json',json.dumps({**result['manifest'],'revision':result.get('revision',1),'status':result.get('status','待复核')},ensure_ascii=False,indent=2))
        evidence={'counts':result['counts'],'views':result['views'],'quarantine':result['quarantine']}
        if result.get('booking'):
            evidence['booking']={'summary':result['booking']['summary'],
                'containers':result['booking']['containers'],
                'allocations':result['booking']['allocations'],
                'receipt_duplicates':result['booking']['receipt_duplicates'],
                'seal':result['booking']['seal']}
        archive.writestr('evidence.json',json.dumps(evidence,ensure_ascii=False,indent=2))
    return buffer.getvalue()
