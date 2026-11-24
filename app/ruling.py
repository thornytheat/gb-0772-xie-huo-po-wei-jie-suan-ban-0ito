from collections import defaultdict
from datetime import UTC,datetime

from app.archive.canonical import digest
from app.billing.rates import price
from app.curb.timeaxis import LOCAL,M,count,minute,span
from app.problems import DocketError


def groups(disputes,sample):
    buckets=defaultdict(list)
    for row in disputes:
        buckets[row['berth'],row['code']].append(row['at'])
    result=[]
    for (berth,code),minutes in sorted(buckets.items()):
        ordered=sorted(minutes)
        start=previous=ordered[0]
        for when in ordered[1:]:
            if when!=previous+M:
                result.append(_group(sample,berth,code,start,previous+M))
                start=when
            previous=when
        result.append(_group(sample,berth,code,start,previous+M))
    return result


def _group(sample,berth,code,start,end):
    key=digest({'sample':sample,'berth':berth,'code':code,
                'start':start.isoformat(),'end':end.isoformat()})
    return {'dispute_key':key,'berth':berth,'code':code,'start':start,'end':end,'minutes':count(start,end)}


def involved(stays,target):
    return {s['vehicle'] for s in stays
            if s['berth']==target['berth'] and s['start']<target['end'] and s['end']>target['start']}


def plan(clearing,sample,dispute_key,vehicle,shift,decided_at=None):
    target=next((g for g in groups(clearing.occupancy['disputes'],sample) if g['dispute_key']==dispute_key),None)
    if target is None:
        raise DocketError('RULING_BAD','没有对应的争议组',422)
    if vehicle not in involved(clearing.identity['stays'],target):
        raise DocketError('RULING_BAD','车辆代号不涉及该争议',422)
    decided=decided_at or datetime.now(UTC)
    ruling={'sample':sample,'dispute_key':dispute_key,'berth':target['berth'],'code':target['code'],
            'start':target['start'].isoformat(),'end':target['end'].isoformat(),
            'vehicle':vehicle,'shift':shift,'decided_at':decided.isoformat(timespec='seconds')}
    ruling['ruling_key']=digest({'dispute':dispute_key,'vehicle':vehicle,'shift':shift})
    return ruling


def award_minutes(ruling):
    start,end=minute(ruling['start']),minute(ruling['end'])
    return {f"{ruling['vehicle']}|{when.isoformat()}" for when in span(start,end)}


def reconcile(clearing,sample,rulings):
    stays=clearing.identity['stays']
    disputes=clearing.coverage[1]
    current={s['stay']:s['due_cents'] for s in price(stays,disputes)}
    original=dict(current)
    restored=set()
    rows=[]
    for ruling in rulings:
        start,end=minute(ruling['start']),minute(ruling['end'])
        restored|={(ruling['berth'],when,ruling['vehicle']) for when in span(start,end)}
        after={s['stay']:s['due_cents'] for s in price(stays,disputes,restored)}
        stay=next((s for s in stays if s['vehicle']==ruling['vehicle'] and s['berth']==ruling['berth']
                   and s['start']<end and s['end']>start),None)
        if stay is None:
            continue
        released=sorted({(s['berth'],when) for s in stays
                         if s['vehicle']==ruling['vehicle'] and s['berth']!=ruling['berth']
                         for when in span(max(s['start'],start),min(s['end'],end))
                         if (s['berth'],when) in disputes})
        rows.append({'ruling':ruling,'stay':stay['stay'],
                     'before_cents':current[stay['stay']],'after_cents':after[stay['stay']],
                     'supplement_cents':after[stay['stay']]-current[stay['stay']],
                     'tier_effect_cents':sum(after[key]-current[key] for key in after if key!=stay['stay']),
                     'released':[{'berth':berth,'at':when.isoformat()} for berth,when in released]})
        current=after
    totals={'original_due_cents':sum(original.values()),'repriced_due_cents':sum(current.values()),
            'supplement_cents':sum(row['supplement_cents'] for row in rows),
            'tier_effect_cents':sum(row['tier_effect_cents'] for row in rows)}
    balanced=totals['repriced_due_cents']-totals['original_due_cents']==totals['supplement_cents']+totals['tier_effect_cents']
    return {'rows':rows,'totals':totals,'balanced':balanced}


def supplement_of(ruling,row):
    decided=datetime.fromisoformat(ruling['decided_at'])
    return {'ruling_key':ruling['ruling_key'],'dispute_key':ruling['dispute_key'],'sample':ruling['sample'],
            'vehicle':ruling['vehicle'],'shift':ruling['shift'],'stay':row['stay'],
            'supplement_cents':row['supplement_cents'],'decided_at':ruling['decided_at'],
            'posted_day':decided.astimezone(LOCAL).date().isoformat()}
