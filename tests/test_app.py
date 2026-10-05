import importlib.util
import sys
sys.path.insert(0,str(__import__('pathlib').Path(__file__).resolve().parents[1]))
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, build_opener, HTTPCookieProcessor
from urllib.error import HTTPError
from http.cookiejar import CookieJar
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('linkcart',Path(__file__).resolve().parents[1]/'server.py')
app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app)

class FlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();app.DB=Path(cls.tmp.name)/'test.sqlite';app.ADMIN_TOKEN='unit-test-staff';app.DEMO_ENABLED=True;app.SERP_KEY='';app.init_db()
        cls.server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        cls.base='http://127.0.0.1:'+str(cls.server.server_port);app.BASE_URL=cls.base
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.tmp.cleanup()
    def setUp(self):
        app.RATE_BUCKETS.clear()
        self.client=build_opener(HTTPCookieProcessor(CookieJar()))
        self.other=build_opener(HTTPCookieProcessor(CookieJar()))
    def request(self,path,data=None,staff=False,client=None,headers=None):
        h={'Content-Type':'application/json'};h.update(headers or {})
        if staff:h['Authorization']='Bearer unit-test-staff'
        req=Request(self.base+path,data=None if data is None else json.dumps(data).encode(),headers=h)
        try:
            with (client or self.client).open(req) as r:return r.status,json.load(r)
        except HTTPError as e:return e.code,json.load(e)
    def catalog(self):
        fixture={'title':'Test product','demo':False,'warnings':[],'offers':[{'id':'0','title':'Test product','price_cents':24800,'checkout_available':True,'demo':False,'url':'https://www.amazon.com/dp/B09XS7JWHH'}]}
        with patch.object(app,'live_search',return_value=fixture):
            code,catalog=self.request('/api/search',{'query':'Test product'})
        self.assertEqual(code,200)
        return catalog
    def checkout(self,extra=None):
        catalog=self.catalog()
        data={'search_id':catalog['search_id'],'offer_id':'0','idempotency':'aaaaaaaa-bbbb-cccc-dddd-'+app.secrets.token_hex(6),'customer':{'name':'Test Customer','email':'customer@example.com','phone':'+85512345678','address':'Test street, Phnom Penh'},'quantity':1,'variant':'Silver','consent':True,'payment_method':'abapay_khqr'}
        data.update(extra or {});code,result=self.request('/api/checkout',data)
        return code,result,data
    def test_durable_checkout_and_idempotency(self):
        code,res,data=self.checkout({'price_cents':1,'status':'paid'})
        self.assertEqual(code,201);order=res['order'];self.assertEqual(order['status'],'pending_payment');self.assertEqual(order['item_cents'],24800)
        code,repeated=self.request('/api/checkout',data);self.assertEqual(repeated['order']['id'],order['id'])
        _,orders=self.request('/api/orders');self.assertEqual(len(orders['orders']),1)
        with app.connect() as con:self.assertEqual(con.execute('SELECT total_cents FROM orders WHERE id=?',(order['id'],)).fetchone()[0],order['total_cents'])
    def test_customer_isolation(self):
        _,res,_=self.checkout();id=res['order']['id']
        _,other=self.request('/api/orders',client=self.other);self.assertEqual(other['orders'],[])
        code,_=self.request('/api/demo/pay',{'order_id':id},client=self.other);self.assertEqual(code,404)
    def test_staff_auth_and_transitions(self):
        code,_=self.request('/api/admin/orders');self.assertEqual(code,401)
        _,res,_=self.checkout();id=res['order']['id']
        code,_=self.request('/api/admin/update',{'order_id':id,'status':'paid'},staff=True);self.assertEqual(code,409)
        code,_=self.request('/api/demo/pay',{'order_id':id});self.assertEqual(code,200)
        code,_=self.request('/api/admin/update',{'order_id':id,'status':'shipped'},staff=True);self.assertEqual(code,409)
        code,_=self.request('/api/admin/update',{'order_id':id,'status':'purchased'},staff=True);self.assertEqual(code,200)
        code,_=self.request('/api/admin/update',{'order_id':id,'status':'shipped'},staff=True);self.assertEqual(code,400)
        code,_=self.request('/api/admin/update',{'order_id':id,'status':'shipped','tracking':'Test carrier 123','note':'Private note'},staff=True);self.assertEqual(code,200)
        _,orders=self.request('/api/orders');self.assertEqual(orders['orders'][0]['tracking'],'Test carrier 123');self.assertNotIn('staff_note',orders['orders'][0])
        code,_=self.request('/api/admin/update',{'order_id':id,'status':'delivered'},staff=True);self.assertEqual(code,200)
    def test_input_validation_and_quote_ownership(self):
        self.assertEqual(self.checkout({'quantity':0})[0],400)
        self.assertEqual(self.checkout({'quantity':True})[0],400)
        self.assertEqual(self.checkout({'consent':False})[0],400)
        self.assertEqual(self.checkout({'payment_method':'bank-transfer'})[0],400)
        catalog=self.catalog()
        code,_=self.request('/api/quote',{'search_id':catalog['search_id'],'offer_id':'0'},client=self.other);self.assertEqual(code,409)
        code,_=self.request('/api/checkout',{'search_id':'missing','idempotency':'a'*32});self.assertEqual(code,409)
    def test_demo_is_explicit_and_live_urls_reach_live_search(self):
        code,_=self.request('/api/search',{'query':'Not a real sample product','demo':True});self.assertEqual(code,410)
        with patch.object(app,'live_search',return_value={'title':'Real product','offers':[],'demo':False,'warnings':[]}) as search:
            code,result=self.request('/api/search',{'query':'https://www.amazon.com/dp/B09XS7JWHH'})
            self.assertEqual(code,200);self.assertFalse(result['demo']);search.assert_called_once()
        code,_=self.request('/api/search',{'query':'https://localhost/product'});self.assertEqual(code,400)
    def test_database_initializes_without_main_entrypoint(self):
        temporary=Path(self.tmp.name)/('lazy-'+app.secrets.token_hex(4)+'.sqlite')
        with patch.object(app,'DB',temporary):
            with app.connect() as con:
                self.assertIsNotNone(con.execute("SELECT name FROM sqlite_master WHERE name='orders'").fetchone())
                self.assertIsNotNone(con.execute("SELECT name FROM sqlite_master WHERE name='events'").fetchone())
    def test_product_details_refresh_updates_authoritative_quote(self):
        catalog=self.catalog()
        data={'search_id':catalog['search_id'],'offer_id':'0'}
        detail={'title':'Test product','native_amount':199,'currency':'USD','native_price':'$199','availability':'In stock','options':{'Color':['Black','White']},'features':['Real feature'],'specifications':{'Model':'T-100'}}
        with patch.object(app,'extract_product',return_value=detail):
            code,result=self.request('/api/product',data)
        self.assertEqual(code,200);self.assertEqual(result['product']['price_cents'],19900)
        self.assertEqual(result['product']['options']['Color'],['Black','White'])
        code,quote=self.request('/api/quote',data)
        self.assertEqual(quote['item_cents'],19900)
        code,_=self.request('/api/product',data,client=self.other);self.assertEqual(code,409)
    def test_product_detail_failure_keeps_search_data_with_notice(self):
        catalog=self.catalog()
        with patch.object(app,'extract_product',side_effect=app.AppError('Blocked')):
            code,result=self.request('/api/product',{'search_id':catalog['search_id'],'offer_id':'0'})
        self.assertEqual(code,200);self.assertEqual(result['product']['price_cents'],24800);self.assertTrue(result['notice'])
    def test_vercel_origin_allowed_without_trusting_other_projects(self):
        host='product-search-platform-delta.vercel.app'
        with patch.dict(app.os.environ,{'VERCEL_PROJECT_PRODUCTION_URL':host},clear=True):
            code,_=self.request('/api/search',{'query':''},headers={'Origin':'https://'+host})
            self.assertEqual(code,400)  # reaches input validation, not origin rejection
            code,_=self.request('/api/search',{'query':''},headers={'Origin':'https://another-project.vercel.app'})
            self.assertEqual(code,403)
            code,_=self.request('/api/search',{'query':''},headers={'Origin':'https://'+host+'.evil.com'})
            self.assertEqual(code,403)
            code,_=self.request('/api/search',{'query':''},headers={'Origin':'https://'+host,'Sec-Fetch-Site':'cross-site'})
            self.assertEqual(code,403)
    def test_base_url_defaults_for_local_and_vercel(self):
        self.assertEqual(app.configured_base_url({},5173),'http://localhost:5173')
        env={'VERCEL_PROJECT_PRODUCTION_URL':'product-search-platform-delta.vercel.app','VERCEL_URL':'preview.vercel.app'}
        self.assertEqual(app.configured_base_url(env,5173),'https://product-search-platform-delta.vercel.app')
        self.assertEqual(app.deployment_origins(env),{'https://product-search-platform-delta.vercel.app','https://preview.vercel.app'})
        self.assertEqual(app.configured_base_url(dict(env,BASE_URL='https://custom.example/'),5173),'https://custom.example')
        self.assertEqual(app.deployment_origins({'VERCEL_URL':'evil.com/path'}),set())
    def test_url_validation(self):
        for url in ['http://amazon.com/x','https://amazon.com.evil.com/x','https://amazon.com:8000/x','https://user:password@amazon.com/x','https://127.0.0.1/x']:
            with self.assertRaises(app.AppError):app.allowed_url(url)
        self.assertEqual(app.allowed_url('https://www.amazon.co.jp/dp/B000000000'),'https://www.amazon.co.jp/dp/B000000000')
    def test_cross_origin_rejected(self):
        code,_=self.request('/api/search',{'query':'Sony WH-1000XM5'},headers={'Origin':'https://other.example'});self.assertEqual(code,403)
    def test_live_payment_amount_and_currency_verification(self):
        _,res,_=self.checkout();id=res['order']['id']
        with app.connect() as con:con.execute('UPDATE orders SET demo=0 WHERE id=?',(id,))
        with app.connect() as con:row=con.execute('SELECT * FROM orders WHERE id=?',(id,)).fetchone()
        payload={'status':{'code':'00','tran_id':id},'data':{'payment_status_code':0,'payment_status':'APPROVED','original_amount':.01,'payment_currency':'USD','refund_amount':0}}
        with patch.object(app,'json_request',return_value=payload):
            with self.assertRaises(app.AppError):app.verify_payway(row)
        payload['data']['original_amount']=row['total_cents']/100;payload['data']['payment_currency']='KHR'
        with patch.object(app,'json_request',return_value=payload):
            with self.assertRaises(app.AppError):app.verify_payway(row)
        payload['data']['payment_currency']='USD';payload['data']['payment_status']='PRE-AUTH'
        with patch.object(app,'json_request',return_value=payload):self.assertEqual(app.verify_payway(row)['status'],'pending_payment')
        payload['data']['payment_status']='APPROVED'
        with patch.object(app,'json_request',return_value=payload):self.assertEqual(app.verify_payway(row)['status'],'paid')
        with patch.object(app,'json_request',side_effect=AssertionError('already verified')):self.assertEqual(app.verify_payway(dict(row,status='paid'))['status'],'paid')
        code,_=self.request('/api/demo/pay',{'order_id':id});self.assertEqual(code,403)
    def test_expired_quote_and_demo_disabled(self):
        catalog=self.catalog()
        app.SEARCHES[catalog['search_id']]['expires']=0
        code,_=self.request('/api/quote',{'search_id':catalog['search_id'],'offer_id':'0'})
        self.assertEqual(code,409)
        with patch.object(app,'DEMO_ENABLED',False):
            code,_=self.request('/api/search',{'query':'Sony WH-1000XM5','demo':True})
            self.assertEqual(code,410)
    def test_valid_callback_is_verified_and_idempotent(self):
        import base64,hashlib,hmac
        _,res,_=self.checkout();id=res['order']['id']
        with app.connect() as con:con.execute('UPDATE orders SET demo=0 WHERE id=?',(id,))
        with app.connect() as con:row=con.execute('SELECT * FROM orders WHERE id=?',(id,)).fetchone()
        callback={'tran_id':id,'status':'0'}
        secret='unit-test-payway-key'
        signature=base64.b64encode(hmac.new(secret.encode(),('0'+id).encode(),hashlib.sha512).digest()).decode()
        response={'status':{'code':'00','tran_id':id},'data':{'payment_status_code':0,'payment_status':'APPROVED','original_amount':row['total_cents']/100,'payment_currency':'USD','refund_amount':0}}
        with patch.object(app,'PAYWAY_READY',True),patch.object(app,'PAYWAY_KEY',secret),patch.object(app,'json_request',return_value=response) as provider:
            code,_=self.request('/api/payway/callback',callback,headers={'X-PayWay-HMAC-SHA512':signature})
            self.assertEqual(code,200);self.assertEqual(provider.call_count,1)
            code,_=self.request('/api/payway/callback',callback,headers={'X-PayWay-HMAC-SHA512':signature})
            self.assertEqual(code,200);self.assertEqual(provider.call_count,1)
        with app.connect() as con:
            self.assertEqual(con.execute('SELECT status FROM orders WHERE id=?',(id,)).fetchone()[0],'paid')
            self.assertEqual(con.execute("SELECT COUNT(*) FROM events WHERE order_id=? AND action='Payment verified with PayWay'",(id,)).fetchone()[0],1)
    def test_invalid_payment_callback_rejected(self):
        with patch.object(app,'PAYWAY_READY',True):
            code,_=self.request('/api/payway/callback',{'tran_id':'fake','status':'0'},headers={'X-PayWay-HMAC-SHA512':'wrong'});self.assertEqual(code,401)

if __name__=='__main__':unittest.main()

class UploadTests(unittest.TestCase):
    def test_decode_and_strip_metadata(self):
        import io,base64
        from PIL import Image
        out=io.BytesIO();Image.new('RGB',(1500,1000),'red').save(out,format='PNG')
        normalized=app.normalized_image({'image':base64.b64encode(out.getvalue()).decode()})
        self.assertLess(len(normalized),500000)
        with Image.open(io.BytesIO(normalized)) as image:
            self.assertEqual(image.format,'JPEG');self.assertLessEqual(image.width,1200);self.assertFalse(image.getexif())
    def test_invalid_image(self):
        import base64
        for raw in ['bad base64',base64.b64encode(b'<svg>not an image</svg>').decode()]:
            with self.assertRaises(app.AppError):app.normalized_image({'image':raw})
    def test_catalog_empty_and_mock_removed_in_live_mode(self):
        self.assertFalse(hasattr(app,'demo_catalog'))
    def test_visual_matches_are_actual_provider_results(self):
        import io,base64
        from PIL import Image
        out=io.BytesIO();Image.new('RGB',(10,10)).save(out,format='PNG')
        response=__import__('unittest.mock',fromlist=['MagicMock']).MagicMock()
        response.__enter__.return_value.read.return_value=b'{"image_id":"provider-image"}'
        matches={'visual_matches':[{'title':'Camera','link':'https://www.amazon.com/dp/B000000000','source':'Amazon','price':{'value':'$100.00'},'thumbnail':'https://example.com/camera.jpg'}]}
        with patch.object(app,'SERP_KEY','test-key'),patch.object(app,'urlopen',return_value=response),patch.object(app,'json_request',return_value=matches):
            result=app.image_search({'image':base64.b64encode(out.getvalue()).decode()})
        self.assertEqual(result['offers'][0]['price_cents'],10000);self.assertFalse(result['demo'])
