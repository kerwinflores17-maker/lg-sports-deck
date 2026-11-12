"""Superuser-only *fictional* sender/payment QR settings; no real configuration."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import date
import base64
import io
from pathlib import Path
import subprocess

from PIL import Image
import pytest

from lg_sports_deck.core.domain import Conflict, PermissionDenied
from lg_sports_deck.unified.integration_settings_test import FictionalIntegrationSettings, PAYMENT_TEST_PREFIX
from lg_sports_deck.unified.tests.test_unified import (
    world, client, login, register, create_staff, grants, fixture_players,
)


def decode_image(data_url):
    prefix='data:image/png;base64,'
    assert data_url.startswith(prefix)
    image=Image.open(io.BytesIO(base64.b64decode(data_url[len(prefix):],validate=True))).convert('RGBA')
    decoder=Path(__file__).resolve().parents[1]/'static/vendor/jsQR-1.4.0.js'
    script='''const decode=require(process.argv[1]);const chunks=[];
      process.stdin.on('data',b=>chunks.push(b));process.stdin.on('end',()=>{
      const result=decode(new Uint8ClampedArray(Buffer.concat(chunks)),Number(process.argv[2]),Number(process.argv[3]));
      if(!result)process.exit(2);process.stdout.write(result.data);});'''
    result=subprocess.run(['node','-e',script,str(decoder),str(image.width),str(image.height)],
                          input=image.tobytes(),capture_output=True,check=True,timeout=15)
    return result.stdout.decode('ascii')


def test_settings_are_superuser_only_and_reject_real_data_and_secrets(world):
    path='/api/u/test-integration-settings'
    with ExitStack() as stack:
        owner=stack.enter_context(client(world));admin=stack.enter_context(client(world));manager=stack.enter_context(client(world));player=stack.enter_context(client(world))
        assert owner.get(path).status_code==401
        oh=login(owner,'owner@example.invalid')
        aid=create_staff(owner,oh,'admin@example.invalid','admin');mid=create_staff(owner,oh,'manager@example.invalid','manager')
        all_grants=owner.get('/api/staff').json()['grantable']
        assert grants(owner,oh,aid,all_grants).status_code==200
        assert grants(owner,oh,mid,all_grants).status_code==200
        ah=login(admin,'admin@example.invalid');mh=login(manager,'manager@example.invalid')
        ph,_=register(player,'player@example.invalid','Fictional Player')
        initial=owner.get(path);assert initial.status_code==200 and initial.headers['cache-control']=='no-store'
        data=initial.json();assert data['revision']==0 and data['sender_email'] is None and data['payment_qr_image'] is None
        for c,h in ((admin,ah),(manager,mh),(player,ph)):
            assert c.get(path,headers={'X-Role':'superuser','X-User-ID':str(world.state.owner)}).status_code==403
            assert c.post(path+'/sender',headers=h,json={'expected_revision':0,'sender_email':'staff@example.invalid'}).status_code==403
            assert c.post(path+'/payment-qr',headers=h,json={'expected_revision':0}).status_code==403
        assert owner.post(path+'/sender',headers={'Origin':'https://attacker.invalid','X-CSRF-Token':oh['X-CSRF-Token']},json={'expected_revision':0,'sender_email':'test@example.invalid'}).status_code==403
        assert owner.post(path+'/sender',headers={'Origin':oh['Origin']},json={'expected_revision':0,'sender_email':'test@example.invalid'}).status_code==403
        assert owner.post(path+'/sender',headers=oh,json={'expected_revision':0,'sender_email':'real@gmail.com'}).status_code==422
        assert owner.post(path+'/sender',headers=oh,json={'expected_revision':0,'sender_email':'test@example.invalid','app_password':'never-accepted'}).status_code==422
        assert owner.post(path+'/payment-qr',headers=oh,json={'expected_revision':0,'real_qr_image':'never-accepted'}).status_code==422
        assert owner.post(path+'/payment-qr',headers=oh,json={'expected_revision':True}).status_code==422
        saved=owner.post(path+'/sender',headers=oh,json={'expected_revision':0,'sender_email':'  Sender@EXAMPLE.INVALID  '})
        assert saved.status_code==200 and saved.headers['cache-control']=='no-store'
        assert saved.json()['sender_email']=='sender@example.invalid' and saved.json()['revision']==1
        with world.state.engine.db.read() as c:
            audit=[dict(r) for r in c.execute("SELECT * FROM auth_audit WHERE event LIKE 'unified.test_sender%'")]
            assert len(audit)==1 and audit[0]['actor_id']==world.state.owner
            assert 'sender@example.invalid' not in str(audit) and 'never-accepted' not in str(audit)
        cleared=owner.post(path+'/sender',headers=oh,json={'expected_revision':1,'sender_email':''})
        assert cleared.status_code==200 and cleared.json()['sender_email'] is None and cleared.json()['revision']==2


def test_sample_payment_qr_can_be_replaced_by_superuser_but_cannot_pay(world):
    path='/api/u/test-integration-settings'
    with client(world) as owner:
        oh=login(owner,'owner@example.invalid')
        first=owner.post(path+'/payment-qr',headers=oh,json={'expected_revision':0})
        assert first.status_code==200 and first.json()['revision']==1
        payload1=decode_image(first.json()['payment_qr_image'])
        assert payload1.startswith(PAYMENT_TEST_PREFIX+':') and len(payload1.split(':')[1])==32
        assert 'GCASH' not in payload1.upper() and 'HTTP' not in payload1.upper()
        reread=owner.get(path).json();assert reread['payment_qr_image']==first.json()['payment_qr_image']
        stale=owner.post(path+'/payment-qr',headers=oh,json={'expected_revision':0})
        assert stale.status_code==409
        second=owner.post(path+'/payment-qr',headers=oh,json={'expected_revision':1})
        assert second.status_code==200 and second.json()['revision']==2
        payload2=decode_image(second.json()['payment_qr_image'])
        assert payload2.startswith(PAYMENT_TEST_PREFIX+':') and payload2!=payload1
        assert owner.get(path).json()['payment_qr_image']==second.json()['payment_qr_image']
        with world.state.engine.db.read() as c:
            rows=[dict(r) for r in c.execute("SELECT * FROM auth_audit WHERE event='unified.test_payment_qr_replaced'")]
            assert len(rows)==2 and all(payload1 not in str(r) and payload2 not in str(r) for r in rows)
            assert c.execute('SELECT count(*) FROM unified_bookings').fetchone()[0]==0
            assert c.execute('SELECT count(*) FROM unified_test_mail_jobs').fetchone()[0]==0


def test_competing_fictional_settings_changes_detect_stale_revision(world):
    settings=world.state.test_integration_settings;owner=world.state.owner
    def rotate(_):
        try:return settings.replace_payment_qr(owner,0)['revision']
        except Conflict:return 'stale'
    with ThreadPoolExecutor(max_workers=2) as pool:result=list(pool.map(rotate,range(2)))
    assert sorted(result,key=str)==[1,'stale']
    assert settings.view(owner)['revision']==1
    p=fixture_players(world,1)[0]
    with pytest.raises(PermissionDenied):settings.view(p)
    with pytest.raises(PermissionDenied):settings.replace_payment_qr(p,1)


def test_settings_upgrade_is_additive_and_does_not_change_simulated_bookings_or_mail(world):
    svc=world.state.unified;owner=world.state.owner;p=fixture_players(world,1)[0]
    booking=svc.book_court(p,date(2027,6,2),12,1,1,[p])
    with world.state.engine.db.tx() as c:
        c.execute('DROP TABLE unified_test_integration_settings')
    settings=FictionalIntegrationSettings(svc)
    assert settings.view(owner)['revision']==0
    settings.replace_sender(owner,0,'temporary@example.invalid')
    with world.state.engine.db.read() as c:
        assert c.execute('SELECT amount FROM unified_bookings WHERE id=?',(booking['booking_id'],)).fetchone()[0]==250
        job=c.execute('SELECT recipient,state FROM unified_test_mail_jobs').fetchone()
        assert tuple(job)==('p0@example.invalid','queued')  # not sent; fake sender does not connect to SMTP
        assert c.execute('SELECT count(*) FROM unified_bookings').fetchone()[0]==1
