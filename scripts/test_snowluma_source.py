"""Synthetic local tests only. Never sends a QQ or Calendar request."""
import copy
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
import pipeline as p
import snowluma_source as s


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        p.save(self.root/'.private/config.json', {'qq_account':'123456789', 'calendar_id':'primary', 'qq_path':r'C:\Example\QQ.exe', 'snowluma_terms_accepted':True, 'backend_cache_acknowledged':True})
        self.floor=int(time.time())-10
        p.save(self.root/'.private/message-source.json',{'source':'snowluma_push_new_only','authorization_question_id':'synthetic-authorization',
            'automatic_qq_recovery':False,'capture_start_epoch':self.floor,'capture_start_at':s.now()})
        p.save(self.root/'.private/account-selection.json',{'group_allowlist':['group:100'],'private_allowlist':['user:200']})
        p.save(self.root/'.private/snowluma-secrets.json',{'push_token':'synthetic-secret','onebot_token':'synthetic-local-only'})
        self.event={'self_id':int(s.account(self.root)),'post_type':'message','message_type':'group','group_id':100,
           'time':int(time.time()),'user_id':200,'message_seq':10,'message_id':123,
           'message':[{'type':'text','data':{'text':'synthetic fixture'}}],'sender':{'user_id':200}}
    def tearDown(self):self.temp.cleanup()
    def count(self,table):
        with s.db_open(self.root) as c:return c.execute('SELECT count(*) FROM '+table).fetchone()[0]
    def health(self):return {'healthy':True,'quarantined':0,'gaps':[],'capture_start_epoch':self.floor}
    def test_scope_and_account_dropped_before_raw_storage(self):
        for field,value in [('group_id',999),('self_id',999)]:
            e={**self.event,field:value};s.ingest(e,self.root)
        self.assertEqual(self.count('delivery'),0);self.assertEqual(self.count('quarantine'),0)
    def test_offline_heartbeat_preserves_first_gap_until_recovery(self):
        e={'self_id':int(s.account(self.root)),'post_type':'meta_event','meta_event_type':'heartbeat','status':{'online':True,'good':True}}
        s.ingest(e,self.root)
        with s.db_open(self.root) as c:first=s.meta(c,'heartbeat_at')
        e['status']['online']=False;s.ingest(e,self.root);s.ingest(e,self.root)
        with s.db_open(self.root) as c:
            rows=c.execute('SELECT * FROM gap').fetchall()
            self.assertEqual(len(rows),1);self.assertEqual(rows[0]['first_uncertain_at'],first)
            self.assertIsNone(rows[0]['resumed_at'])
        e['status']['online']=True;s.ingest(e,self.root)
        with s.db_open(self.root) as c:self.assertIsNotNone(c.execute('SELECT resumed_at FROM gap').fetchone()[0])
    def test_old_receiver_heartbeat_cannot_prove_restarted_receiver_ready(self):
        with s.db_open(self.root) as c:
            s.put(c,'heartbeat_at',s.now());s.put(c,'backend_online',True)
        with s.db_open(self.root) as c:s.put(c,'receiver_started_at',s.now())
        self.assertFalse(s.status(self.root,probe=False)['healthy'])
    def test_activation_floor_excludes_old_messages(self):
        self.assertEqual(s.ingest({**self.event,'time':self.floor-1},self.root),'before_activation')
        self.assertEqual(self.count('delivery'),0)
    def test_replay_deduplicates_and_changes_create_version(self):
        self.assertEqual(s.ingest(self.event,self.root),'stored')
        self.assertEqual(s.ingest(self.event,self.root),'duplicate')
        e=copy.deepcopy(self.event);e['message'][0]['data']['text']='updated synthetic fixture'
        self.assertEqual(s.ingest(e,self.root),'stored')
        self.assertEqual(self.count('current_event'),1);self.assertEqual(self.count('delivery'),2)
    def test_unknown_identity_is_quarantined_not_mapped(self):
        e={**self.event};del e['message_seq']
        self.assertEqual(s.ingest(e,self.root),'quarantined')
        self.assertEqual(self.count('delivery'),0);self.assertEqual(self.count('quarantine'),1)
    def test_own_private_reply_uses_peer_and_sender_identity(self):
        incoming={**self.event,'message_type':'private'};incoming.pop('group_id')
        outgoing={**incoming,'post_type':'message_sent','user_id':int(s.account(self.root)),'target_id':200}
        self.assertEqual(s.ingest(incoming,self.root),'stored')
        self.assertEqual(s.ingest(outgoing,self.root),'stored')
        with s.db_open(self.root) as c:
            self.assertEqual({r[0] for r in c.execute('SELECT session FROM delivery')},{'user:200'})
        self.assertEqual(self.count('delivery'),2)
    def test_http_auth_and_empty_response_cannot_trigger_quick_operations(self):
        server=s.make_receiver(self.root,0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            url='http://127.0.0.1:'+str(server.server_port)+'/events'
            raw=json.dumps(self.event).encode();signature='sha1='+hmac.new(b'synthetic-secret',raw,hashlib.sha1).hexdigest()
            headers={'Content-Type':'application/json','X-Self-ID':s.account(self.root),'X-Signature':'invalid'}
            try:
                urllib.request.urlopen(urllib.request.Request(url,data=raw,headers=headers))
                self.fail('Unsigned request was accepted')
            except urllib.error.HTTPError as error:
                self.assertEqual(error.code,401);error.close()
            self.assertEqual(self.count('delivery'),0)
            headers['X-Signature']=signature
            with urllib.request.urlopen(urllib.request.Request(url,data=raw,headers=headers)) as r:
                self.assertEqual(r.status,204);self.assertEqual(r.read(),b'');self.assertIsNone(r.headers.get('Content-Type'))
            self.assertEqual(self.count('delivery'),1)
        finally:server.shutdown();server.server_close();thread.join()
    def test_import_replay_and_cursor_transaction(self):
        s.ingest(self.event,self.root)
        self.assertFalse(hasattr(p,'OneBot'))
        with patch.object(s,'status',return_value=self.health()):
            first=p.collect(self.root);second=p.collect(self.root)
        self.assertEqual(first['sessions']['group:100']['new_or_changed'],1)
        self.assertEqual(second['sessions']['group:100']['new_or_changed'],0)
        with p.open_db(self.root) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM message').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT last_delivery FROM provider_cursor').fetchone()[0],1)
        self.assertEqual(len(p.review_input(self.root)),1)
    def test_failed_source_does_not_import_or_advance(self):
        s.ingest(self.event,self.root)
        with patch.object(s,'status',return_value={**self.health(),'healthy':False}):
            with self.assertRaises(ValueError):p.collect(self.root)
        with p.open_db(self.root) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM message').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM provider_cursor').fetchone()[0],0)
        self.assertEqual(p.review_input(self.root),[])
    def test_import_failure_rolls_back_messages_and_cursor_together(self):
        s.ingest(self.event,self.root)
        with p.open_db(self.root) as c:
            c.execute("CREATE TRIGGER synthetic_failure BEFORE INSERT ON message_source BEGIN SELECT RAISE(ABORT,'synthetic import failure'); END")
        with patch.object(s,'status',return_value=self.health()):
            with self.assertRaises(Exception):p.collect(self.root)
        with p.open_db(self.root) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM message').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM provider_cursor').fetchone()[0],0)
            c.execute('DROP TRIGGER synthetic_failure')
        with patch.object(s,'status',return_value=self.health()):
            result=p.collect(self.root)
        self.assertEqual(result['sessions']['group:100']['new_or_changed'],1)
    def test_old_pending_messages_not_exported_as_new(self):
        with p.open_db(self.root) as c:
            c.execute('INSERT INTO message VALUES(?,?,?,?,?,?,?,?,?,?)',('legacy','group:100','1','1',1,'200','old','{}',None,'old-run'))
        with patch.object(s,'status',return_value=self.health()):p.collect(self.root)
        self.assertEqual(p.review_input(self.root),[])
    def test_acceptance_requires_real_captured_key_and_question(self):
        p.save(self.root/'state/snowluma/acceptance.json',{'group':{'user_confirmed':True,'source_question_id':'synthetic-question','event_key':'missing'}})
        self.assertEqual(s.accepted_types(self.root),set())
        s.ingest(self.event,self.root);key=s.normalize('group:100',self.event,self.root)['key']
        p.save(self.root/'state/snowluma/acceptance.json',{'group':{'user_confirmed':True,'source_question_id':'synthetic-question','event_key':key}})
        self.assertEqual(s.accepted_types(self.root),{'group'})
    def test_unknown_provider_does_not_fall_back(self):
        p.save(self.root/'.private/message-source.json',{'source':'unknown'})
        with self.assertRaises(ValueError):p.collect(self.root)
    def test_calendar_requires_confirmed_kind_and_fresh_online_source(self):
        s.ingest(self.event,self.root)
        with patch.object(s,'status',return_value=self.health()):p.collect(self.root)
        row=p.review_input(self.root)[0]
        task={'id':'c'*24,'status':'active','title':'synthetic','sources':[row['key']],
          'date_explicit':True,'personally_required':True,'event':{'title':'synthetic','timezone_str':'Asia/Shanghai',
          'start_date':'2026-10-24','end_date':'2026-10-25'}}
        p.apply_review({'reviewed':[{'key':row['key'],'fingerprint':row['fingerprint']}],'tasks':[task]},self.root)
        with patch.object(s,'status',return_value=self.health()):self.assertEqual(p.calendar_plan(self.root),[])
        evidence={'source_question_id':'synthetic-comparison','user_confirmed':True,'user_answer':'synthetic positive observation','event_key':row['key']}
        s.confirm_natural(evidence,self.root)
        with patch.object(s,'status',return_value=self.health()):planned=p.calendar_plan(self.root)
        self.assertEqual(len(planned),1)
        with patch.object(s,'status',return_value={**self.health(),'healthy':False}):
            with self.assertRaises(ValueError):p.operation_start(planned[0]['operation_id'],self.root)


if __name__=='__main__':unittest.main()
