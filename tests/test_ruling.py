from concurrent.futures import ThreadPoolExecutor
from datetime import UTC,datetime
from types import SimpleNamespace

import pytest

from app.archive.docket import Docket
from app.clearing import Clearing
from app.problems import DocketError
from app.ruling import award_minutes,groups,plan,reconcile
from app.source.packet import bundle

SAMPLE='dirty'


@pytest.fixture(scope='module')
def clearing():
    return Clearing(bundle(SAMPLE),b'fixture-2026')


@pytest.fixture(scope='module')
def dispute(clearing):
    return {g['berth']:g for g in groups(clearing.occupancy['disputes'],SAMPLE)}


def vehicle_with_due(clearing,berth,due):
    return next(s['vehicle'] for s in clearing.pricing['stays'] if s['berth']==berth and s['due_cents']==due)


def submit(client,dispute_key,vehicle,shift='夜班'):
    return client.post('/api/rulings',json={'sample':SAMPLE,'dispute_key':dispute_key,'vehicle':vehicle,'shift':shift})


def involved_stays(client,group):
    stays=client.get('/api/cases/dirty/pricing').json()['stays']
    return [s for s in stays if s['berth']==group['berth'] and s['start']<group['end'] and s['end']>group['start']]


def test_disputes_group_into_ruling_targets(dispute):
    assert [(g['code'],g['minutes']) for g in dispute.values()]==[('PLATE_OVERLAP',5),('BERTH_OVERLAP',2)]
    assert len({g['dispute_key'] for g in dispute.values()})==2


def test_award_restores_early_segment_at_own_tier(clearing,dispute):
    vehicle=vehicle_with_due(clearing,'B-01',700)
    ruling=plan(clearing,SAMPLE,dispute['B-01']['dispute_key'],vehicle,'夜班')
    row=reconcile(clearing,SAMPLE,[ruling])['rows'][-1]
    assert (row['before_cents'],row['after_cents'],row['supplement_cents'])==(700,900,200)
    assert row['tier_effect_cents']==0
    assert [r['berth'] for r in row['released']]==['B-02','B-02']


def test_award_reprices_whole_disputed_order(clearing,dispute):
    vehicle=vehicle_with_due(clearing,'B-01',60)
    ruling=plan(clearing,SAMPLE,dispute['B-01']['dispute_key'],vehicle,'夜班')
    row=reconcile(clearing,SAMPLE,[ruling])['rows'][-1]
    assert (row['before_cents'],row['after_cents'],row['supplement_cents'])==(60,240,180)


def test_tier_push_stays_out_of_supplement():
    at=lambda hour,minute:datetime(2026,3,1,hour,minute,tzinfo=UTC)
    stay=lambda key,start,end:{'stay':key,'berth':'B-01','vehicle':'CAR','start':start,'end':end,'booking':None}
    first,second=stay('S1',at(10,0),at(10,10)),stay('S2',at(10,30),at(10,50))
    disputes={('B-01',at(10,m)):'PLATE_OVERLAP' for m in range(5,10)}
    clearing=SimpleNamespace(identity={'stays':[first,second]},coverage=(None,disputes))
    ruling={'sample':'x','dispute_key':'d','berth':'B-01','vehicle':'CAR','shift':'早班',
            'start':at(10,5).isoformat(),'end':at(10,10).isoformat()}
    book=reconcile(clearing,'x',[ruling])
    row=book['rows'][-1]
    assert (row['before_cents'],row['after_cents'],row['supplement_cents'])==(100,200,100)
    assert row['tier_effect_cents']==100
    assert book['totals']['supplement_cents']==100 and book['balanced']


def test_ruling_requires_involved_vehicle(clearing,dispute):
    with pytest.raises(DocketError) as failure:
        plan(clearing,SAMPLE,dispute['B-01']['dispute_key'],'未涉及车辆','夜班')
    assert failure.value.code=='RULING_BAD'


def test_submit_and_retry_never_double_posts(client):
    key=client.get('/api/rulings/dirty').json()['disputes'][0]['dispute_key']
    vehicle=client.get('/api/cases/dirty/pricing').json()['stays'][0]['vehicle']
    first,second=submit(client,key,vehicle),submit(client,key,vehicle)
    assert (first.status_code,second.status_code)==(200,200)
    assert (first.json()['reused'],second.json()['reused'])==(False,True)
    assert first.json()['supplement']==second.json()['supplement']
    book=client.get('/api/rulings/dirty').json()
    assert len(book['rulings'])==1
    assert book['reconciliation']['totals']['supplement_cents']==first.json()['supplement']['supplement_cents']


def test_opposite_rulings_single_winner(client):
    group=client.get('/api/rulings/dirty').json()['disputes'][0]
    early,late=(s['vehicle'] for s in involved_stays(client,group))
    assert submit(client,group['dispute_key'],early,'早班').status_code==200
    conflict=submit(client,group['dispute_key'],late,'晚班')
    assert (conflict.status_code,conflict.json()['code'])==(409,'RULING_CONFLICT')
    assert len(client.get('/api/rulings/dirty').json()['rulings'])==1


def test_concurrent_opposite_rulings_one_takes_effect(tmp_path,clearing,dispute):
    docket=Docket(tmp_path/'parallel.sqlite')
    key=dispute['B-01']['dispute_key']
    vehicles=[vehicle_with_due(clearing,'B-01',due) for due in (700,60)]
    rulings=[plan(clearing,SAMPLE,key,vehicle,shift) for vehicle,shift in zip(vehicles,('早班','晚班'))]
    def attempt(ruling):
        try:
            return docket.rule(ruling,{'ruling_key':ruling['ruling_key']},award_minutes(ruling))[2]
        except DocketError as error:
            return error.code
    with ThreadPoolExecutor(2) as workers:
        results=list(workers.map(attempt,rulings))
    assert sorted(results,key=str)==[False,'RULING_CONFLICT']
    assert len(docket.rulings_for(SAMPLE))==1


def test_same_minute_other_berth_stays_released(client):
    groups_by_berth={g['berth']:g for g in client.get('/api/rulings/dirty').json()['disputes']}
    vehicle=next(s['vehicle'] for s in involved_stays(client,groups_by_berth['B-01']) if s['due_cents']==700)
    assert submit(client,groups_by_berth['B-01']['dispute_key'],vehicle).status_code==200
    conflict=submit(client,groups_by_berth['B-02']['dispute_key'],vehicle)
    assert (conflict.status_code,conflict.json()['code'])==(409,'RULING_CONFLICT')
    book=client.get('/api/rulings/dirty').json()['reconciliation']
    assert book['rows'][0]['released'] and len(book['rows'])==1


def test_settled_bills_never_change(client):
    before=client.get('/api/cases/dirty/settlement').json()
    key=client.get('/api/rulings/dirty').json()['disputes'][0]['dispute_key']
    vehicle=client.get('/api/cases/dirty/pricing').json()['stays'][0]['vehicle']
    assert submit(client,key,vehicle).status_code==200
    after=client.get('/api/cases/dirty/settlement').json()
    assert after==before and after['money']['due_cents']==2240


def test_reconciliation_page_shows_before_after_and_supplement(client):
    key=client.get('/api/rulings/dirty').json()['disputes'][0]['dispute_key']
    vehicle=client.get('/api/cases/dirty/pricing').json()['stays'][0]['vehicle']
    submit(client,key,vehicle)
    page=client.get('/clearing/settlement').text
    assert '改判补差对账' in page and '¥7.00' in page and '¥9.00' in page and '¥2.00' in page
    assert '勾稽已平' in page
