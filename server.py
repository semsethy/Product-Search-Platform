#!/usr/bin/env python3
"""Linkcart prototype. Python 3.10+, standard library only."""
import io, warnings, base64, collections, concurrent.futures, hashlib, hmac, html, http.cookies, ipaddress, json, math, mimetypes, os, re, secrets, socket, sqlite3, threading, time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from html.parser import HTMLParser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, urlencode, urljoin
from urllib.request import Request, urlopen, build_opener, HTTPRedirectHandler
from amazon_regions import REGIONS, AMAZON_DOMAINS, marketplace
from product_search import parse_product, parse_amazon_results, search_query, canonical, asin_from_url, relevance, parse_price, alternative_query

ROOT = Path(__file__).resolve().parent
for line in (ROOT / '.env').read_text().splitlines() if (ROOT / '.env').exists() else []:
    if '=' in line and not line.strip().startswith('#'):
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
PORT = int(os.environ.get('PORT', '5173'))
def deployment_origins(environ):
    # Trust only platform-provided project URLs, never client Host/Origin headers.
    origins=set()
    for key in ['VERCEL_PROJECT_PRODUCTION_URL','VERCEL_URL','VERCEL_BRANCH_URL']:
        host=environ.get(key,'').strip()
        if host and re.fullmatch(r'[a-zA-Z0-9.-]+',host):
            origins.add('https://'+host.lower())
    return origins

def configured_base_url(environ, port):
    explicit=environ.get('BASE_URL','').strip().rstrip('/')
    if explicit: return explicit
    for key in ['VERCEL_PROJECT_PRODUCTION_URL','VERCEL_URL']:
        host=environ.get(key,'').strip()
        candidate='https://'+host.lower()
        if candidate in deployment_origins(environ): return candidate
    return f'http://localhost:{port}'

BASE_URL = configured_base_url(os.environ, PORT)
DB = Path(os.environ.get('DATABASE_PATH', '/tmp/linkcart-test.sqlite3' if os.environ.get('VERCEL') and os.environ.get('PAYMENT_MODE','mock')=='mock' else str(ROOT / 'data' / 'linkcart.sqlite3')))
ADMIN_TOKEN = os.environ.get('ADMIN_TOKEN', '')
SERP_KEY = os.environ.get('SERPAPI_KEY', '')
RAINFOREST_KEY = os.environ.get('RAINFOREST_API_KEY', '')
PAYWAY_ID = os.environ.get('PAYWAY_MERCHANT_ID', '')
PAYWAY_KEY = os.environ.get('PAYWAY_API_KEY', '')
PAYWAY_MODE = os.environ.get('PAYWAY_ENV', 'sandbox')
PAYWAY_BASE = 'https://checkout.payway.com.kh' if PAYWAY_MODE == 'production' else 'https://checkout-sandbox.payway.com.kh'
PAYWAY_READY = bool(PAYWAY_ID and PAYWAY_KEY)
ALLOW_LIVE_CHECKOUT = os.environ.get('ENABLE_LIVE_CHECKOUT', 'false').lower() == 'true'
PAYMENT_MODE = os.environ.get('PAYMENT_MODE', 'mock')
if PAYMENT_MODE not in ['mock', 'sandbox', 'production']: raise RuntimeError('Invalid PAYMENT_MODE')
DEMO_ENABLED = PAYMENT_MODE == 'mock'
PRICING_READY = bool(os.environ.get('SERVICE_FEE_RATE') and os.environ.get('SHIPPING_USD'))
SERVICE_RATE = Decimal(os.environ.get('SERVICE_FEE_RATE', '0.08'))
SHIPPING = Decimal(os.environ.get('SHIPPING_USD', '12.00'))
STATES = ['pending_payment', 'paid', 'purchased', 'shipped', 'delivered']
ALLOWED_STORES = ['amazon.com', 'amazon.co.jp', 'amazon.co.uk', 'amazon.de', 'amazon.sg', 'amazon.in', 'amazon.com.au', 'ebay.com', 'ebay.co.uk', 'rakuten.co.jp', 'walmart.com', 'bestbuy.com', 'sony.com', 'sony.co.jp', 'nike.com', 'adidas.com', 'newbalance.com', 'newbalance.jp', 'uniqlo.com', 'mercari.com', 'zozo.jp', 'amazon.ca', 'amazon.fr', 'amazon.it', 'amazon.es', 'amazon.nl', 'amazon.ae', 'amzn.to', 'amzn.asia', 'amazon.jp', 'a.co', 'target.com', 'bhphotovideo.com', 'adorama.com', 'etsy.com', 'newegg.com', 'apple.com', 'sony.co.uk', 'sony.jp']
ALLOWED_STORES = list(dict.fromkeys(ALLOWED_STORES + list(AMAZON_DOMAINS.values())))
CACHE_LOCK = threading.Lock()
SEARCHES = {}
RATE_BUCKETS = collections.defaultdict(collections.deque)

class AppError(Exception):
    def __init__(self, message, status=400): self.message, self.status = message, status

def money(value):
    return int((Decimal(str(value)) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))

def now(): return datetime.now(timezone.utc).isoformat()

def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    if not con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='orders'").fetchone():
        initialize_schema(con)
    return con

def initialize_schema(con):
    con.executescript('''PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS orders (
          id TEXT PRIMARY KEY, session TEXT NOT NULL, idempotency TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, status TEXT NOT NULL,
          demo INTEGER NOT NULL, offer TEXT NOT NULL, customer TEXT NOT NULL,
          quantity INTEGER NOT NULL, variant TEXT NOT NULL, payment_method TEXT NOT NULL,
          item_cents INTEGER NOT NULL, fee_cents INTEGER NOT NULL, shipping_cents INTEGER NOT NULL,
          total_cents INTEGER NOT NULL, payment_ref TEXT, tracking TEXT DEFAULT '', staff_note TEXT DEFAULT '',
          UNIQUE(session,idempotency));
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,order_id TEXT NOT NULL,created_at TEXT NOT NULL,action TEXT NOT NULL);
        ''')

def init_db():
    with connect() as con:
        initialize_schema(con)

def allowed_url(url):
    p = urlparse(url)
    host = (p.hostname or '').lower()
    if p.scheme != 'https' or p.username or p.password or (p.port and p.port != 443):
        raise AppError('Use a public HTTPS product URL without login details.')
    if not any(host == d or host.endswith('.' + d) for d in ALLOWED_STORES):
        raise AppError('This retailer is not supported yet. Search by product name instead.')
    return url

def public_dns(url):
    allowed_url(url)
    for row in socket.getaddrinfo(urlparse(url).hostname, 443, type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(row[4][0]).is_global:
            raise AppError('Only public retailer addresses are supported.')

class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_dns(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def json_request(url, data=None, timeout=20):
    body = json.dumps(data).encode() if data is not None else None
    req = Request(url, data=body, headers={'Content-Type':'application/json', 'User-Agent':'Linkcart/0.1'})
    with urlopen(req, timeout=timeout) as res:
        raw = res.read(4_000_001)
        if len(raw) > 4_000_000: raise AppError('Provider response was too large.', 502)
        return json.loads(raw)

class ProductParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.meta = {}; self.json_blocks = []; self.in_json = False; self.in_title = False; self.title = ''; self.current = ''
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta': self.meta[attrs.get('property', attrs.get('name',''))] = attrs.get('content','')
        if tag == 'script' and attrs.get('type') == 'application/ld+json': self.in_json = True; self.current = ''
        if tag == 'title': self.in_title = True
    def handle_data(self, data):
        if self.in_json: self.current += data
        if self.in_title: self.title += data
    def handle_endtag(self, tag):
        if tag == 'script' and self.in_json: self.json_blocks.append(self.current); self.in_json = False
        if tag == 'title': self.in_title = False

def find_product(node):
    if isinstance(node, list):
        for child in node:
            result = find_product(child)
            if result: return result
    if isinstance(node, dict):
        t = node.get('@type','')
        if t == 'Product' or isinstance(t, list) and 'Product' in t: return node
        for child in node.values():
            if isinstance(child, (dict,list)):
                result = find_product(child)
                if result: return result
    return None

def retailer_html(url):
    public_dns(url)
    req=Request(url,headers={'User-Agent':'Mozilla/5.0','Accept':'text/html','Accept-Language':'en-US,en;q=0.9'})
    with build_opener(SafeRedirect()).open(req,timeout=14) as res:
        content=res.read(8_000_001)
        if len(content)>8_000_000: raise AppError('This retailer page is too large to process.',422)
        return content.decode('utf-8',errors='replace'),res.url

def extract_product(url):
    allowed_url(url)
    host=urlparse(url).hostname.lower();asin=asin_from_url(url)
    if host in ['amzn.asia','amzn.to','a.co','amazon.jp','www.amazon.jp']:
        try:
            shared_markup,resolved=retailer_html(url)
            resolved_host=(urlparse(resolved).hostname or '').lower()
            if not asin_from_url(resolved) or not any(resolved_host==d or resolved_host.endswith('.'+d) for d in ALLOWED_STORES if d.startswith('amazon.')):
                raise ValueError('Short link did not resolve to an Amazon product.')
        except Exception:
            raise AppError('The Amazon share link could not be resolved. Copy the full product-page URL from your browser.',422)
        if not RAINFOREST_KEY:
            try:return parse_product(shared_markup,resolved)
            except ValueError:pass
        # Retry the canonical detail page if the share page is a challenge.
        return extract_product(canonical(resolved))
    if 'amazon.' in host and asin and RAINFOREST_KEY:
        try:
            data=json_request('https://api.rainforestapi.com/request?'+urlencode({'api_key':RAINFOREST_KEY,'type':'product','amazon_domain':host.removeprefix('www.'),'asin':asin}))
            p=data.get('product',{});price=p.get('buybox_winner',{}).get('price',{})
            if p.get('title'):
                return {'title':p['title'],'model':p.get('model_number',''),'brand':p.get('brand',''),'url':canonical(url),'asin':asin,'image':p.get('main_image',{}).get('link',''),'description':p.get('description',''),'extraction':'Amazon product API','native_amount':price.get('value'),'currency':price.get('currency',''),'native_price':price.get('raw',''),'availability':'Not confirmed'}
        except Exception: pass
    # Request the regional currency explicitly; always parse the returned currency.
    if 'amazon.' in host and asin:
        curr=(marketplace(host) or ('','','','USD'))[3]
        url=canonical(url)+'?'+urlencode({'language':'en_US','currency':curr})
    try:
        markup,final_url=retailer_html(url)
        resolved_asin=asin_from_url(final_url)
        if asin and resolved_asin != asin: raise ValueError('Retailer redirected to a different product.')
        return parse_product(markup,final_url)
    except Exception:
        raise AppError('The retailer did not return readable product content. Try the full product URL instead of a short link, or enter its exact name/model. Some stores require a product-data API.',422)

def shopping_region(query, region):
    code, name, flag, currency = region
    data = json_request('https://serpapi.com/search.json?' + urlencode({'api_key':SERP_KEY,'engine':'google_shopping','q':query,'gl':code,'google_domain':'google.com','hl':'en','num':8}))
    if data.get('error'): raise AppError('Product search provider could not complete this region.',502)
    results=[]
    for p in data.get('shopping_results',[])[:8]:
        price=p.get('extracted_price'); link=p.get('product_link') or p.get('link','')
        if not isinstance(price,(float,int)) or price<=0 or not math.isfinite(price): continue
        if urlparse(link).scheme != 'https' or not urlparse(link).hostname: continue
        _,actual_currency=parse_price(str(p.get('price','')),currency)
        results.append({'title':str(p.get('title','Product'))[:300], 'merchant':str(p.get('source','Retailer'))[:100], 'region':name,'region_code':code,'flag':flag,'native_price':str(p.get('price',price)),'currency':actual_currency,'native_amount':price,'url':link,'rating':p.get('rating'), 'reviews':p.get('reviews'), 'image':p.get('thumbnail','') if str(p.get('thumbnail','')).startswith('https://') else '', 'variant':'Verify size / color with retailer','condition':p.get('second_hand_condition','Not confirmed'),'match':'similar listing','demo':False,'art':'headphone','color':'Black','tone':'sage','source_kind':'Shopping search provider','availability':'Not confirmed','description':p.get('snippet','Similar search result. Model, variant, merchant availability, and destination delivery must be verified. The region is the shopping search market, not a verified warehouse location.'),'checkout_available':False})
    return results

REGIONAL_CACHE={}

def source_offer(product):
    host=urlparse(product['url']).hostname
    region=next((r for r in REGIONS if host==AMAZON_DOMAINS[r[0]] or host.endswith('.'+AMAZON_DOMAINS[r[0]])),None)
    region=region or ('other','Other stores','🌐',product.get('currency',''))
    return {**product,'merchant':'Amazon '+region[1] if 'amazon.' in host else host.removeprefix('www.'),'region_code':region[0],'region':region[1],'flag':region[2],'rating':None,'reviews':None,'variant':'Verify exact color / size','condition':'Verify condition','match':'original product','demo':False,'art':'','color':'','tone':'sage','checkout_available':False,'source_kind':'Original product page','verified_at':now()}

def rainforest_region(product,region):
    domain=AMAZON_DOMAINS[region[0]]
    result=json_request('https://api.rainforestapi.com/request?'+urlencode({'api_key':RAINFOREST_KEY,'type':'search','amazon_domain':domain,'search_term':search_query(product),'number_of_results':12}),timeout=25)
    if result.get('request_info',{}).get('success') is False: raise AppError('Amazon data provider could not search this marketplace.',502)
    offers=[]
    for item in result.get('search_results',[]):
        score=relevance(item.get('title',''),product)
        asin=item.get('asin','')
        if not score or not re.fullmatch(r'[A-Z0-9]{10}',asin):continue
        price=item.get('price') or {}
        amount,curr=parse_price(str(price.get('raw') or price.get('value') or ''),price.get('currency') or region[3])
        offers.append({'title':item['title'],'url':'https://www.'+domain+'/dp/'+asin,'asin':asin,'image':item.get('image',''),'native_amount':amount,'native_price':str(price.get('raw','')),'currency':curr,'merchant':'Amazon '+region[1],'region_code':region[0],'region':region[1],'flag':region[2],'match':'model match' if score==2 else 'similar listing','availability':'Not confirmed','rating':item.get('rating'),'reviews':item.get('ratings_total'),'condition':'Verify condition','variant':'Verify exact color / size','tone':'sage','demo':False,'description':'Amazon product search result. Open details to load specifications and options.','source_kind':'Amazon search API','extraction':'Amazon search API','verified_at':now(),'checkout_available':False})
    return offers[:12]

def direct_amazon_region(product,region):
    if RAINFOREST_KEY:return rainforest_region(product,region)
    domain=AMAZON_DOMAINS[region[0]]
    cache_key=(search_query(product).lower(),region[0])
    with CACHE_LOCK:
        cached=REGIONAL_CACHE.get(cache_key)
        if cached and cached[0]>time.time():return json.loads(json.dumps(cached[1]))
    url='https://www.'+domain+'/s?'+urlencode({'k':search_query(product),'language':'en_US','currency':region[3]})
    markup,_=retailer_html(url)
    offers=parse_amazon_results(markup,domain,region,product)
    broader=alternative_query(product)
    if broader and broader.lower()!=search_query(product).lower() and len(offers)<4:
        try:
            broad_url='https://www.'+domain+'/s?'+urlencode({'k':broader,'language':'en_US','currency':region[3]})
            broad_markup,_=retailer_html(broad_url)
            alternatives=parse_amazon_results(broad_markup,domain,region,product)
            found={o['url'] for o in offers}
            offers.extend(o for o in alternatives if o['url'] not in found)
        except Exception:pass
    offers=offers[:12]
    for offer in offers:offer['verified_at']=now()
    with CACHE_LOCK:
        for key in list(REGIONAL_CACHE):
            if REGIONAL_CACHE[key][0]<time.time():del REGIONAL_CACHE[key]
        if len(REGIONAL_CACHE)<500:REGIONAL_CACHE[cache_key]=(time.time()+120,offers)
    return json.loads(json.dumps(offers))

def regional_product(product,region):
    url='https://www.'+AMAZON_DOMAINS[region[0]]+'/dp/'+product['asin']
    p=extract_product(url)
    if not relevance(p['title'],product):return None
    offer=source_offer(p);offer['match']='model match';offer['source_kind']='Amazon regional product page'
    return offer

def convert_offers(offers,warnings):
    rates={'USD':(1,'')}
    currencies={p.get('currency') for p in offers if p.get('native_amount') is not None and p.get('currency')}
    def fetch_rate(currency):
        data=json_request('https://api.frankfurter.dev/v2/rate/'+currency.lower()+'/usd',timeout=8)
        return float(data['rate']),data.get('date','')
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        tasks={pool.submit(fetch_rate,c):c for c in currencies-{'USD'}}
        for task in concurrent.futures.as_completed(tasks):
            curr=tasks[task]
            try:rates[curr]=task.result()
            except Exception:warnings.append('USD conversion unavailable for '+curr+'. Native prices remain visible.')
    for i,p in enumerate(offers):
        curr=p.get('currency');amount=p.get('native_amount');rate,date=rates.get(curr,(None,''))
        p['id']=str(i)
        p['price_cents']=money(amount*rate) if amount is not None and rate is not None else None
        p['fx_date']=date;p['fx_source']='Frankfurter reference rates' if curr!='USD' else 'USD listing price'
        p['availability']=p.get('availability','Not confirmed');p['verified_at']=p.get('verified_at') or now()
        # Immediate checkout is allowed only for an explicitly in-stock item with a parsed price.
        p['checkout_available']=bool(p['price_cents'] and p['price_cents']<=500000 and (DEMO_ENABLED or (ALLOW_LIVE_CHECKOUT and PAYWAY_READY and PRICING_READY and p['availability']=='In stock')))
    return offers

def live_search(query):
    is_url=query.startswith(('http://','https://'))
    product=extract_product(query) if is_url else {'title':query,'model':'','brand':'','url':'','extraction':'Product name search'}
    offers=[source_offer(product)] if is_url else []
    warnings=[];coverage=[];target_query=search_query(product)
    if not target_query:raise AppError('Could not identify a product name from that link.',422)
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        tasks={pool.submit(shopping_region,target_query,r) if SERP_KEY else pool.submit(direct_amazon_region,product,r):r for r in REGIONS}
        for task in concurrent.futures.as_completed(tasks):
            region=tasks[task]
            try:
                result=task.result()
                coverage.append({'code':region[0],'name':region[1],'flag':region[2],'status':'results' if result else 'no_matches','count':len(result) if isinstance(result,list) else int(bool(result)),'url':'https://www.'+AMAZON_DOMAINS[region[0]]+'/s?'+urlencode({'k':target_query})})
                if isinstance(result,list):offers.extend(result)
                elif result:offers.append(result)
            except Exception:
                coverage.append({'code':region[0],'name':region[1],'flag':region[2],'status':'unavailable','count':0,'url':'https://www.'+AMAZON_DOMAINS[region[0]]+'/s?'+urlencode({'k':target_query})})
                warning=region[1]+' search/page could not be read; this region may be incomplete.'
                if warning not in warnings:warnings.append(warning)
    # Deduplicate by canonical URL, prefer directly read product pages over search results.
    unique={}
    for offer in offers:
        if not offer.get('title') or urlparse(offer.get('url','')).scheme!='https':continue
        if offer.get('match')!='original product' and not relevance(offer['title'],product):continue
        key=canonical(offer['url'])
        previous=unique.get(key)
        if previous is None or offer.get('extraction') in ['Amazon product page','Structured product data','Amazon product API']:
            unique[key]=offer
    offers=list(unique.values())
    to_verify=[]
    for region in REGIONS:
        candidate=next((o for o in offers if o.get('region_code')==region[0] and o.get('extraction')=='Amazon search result' and o.get('match')=='model match'),None)
        if candidate:to_verify.append(candidate)
    if to_verify:
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            futures={pool.submit(extract_product,o['url']):o for o in to_verify}
            for task in concurrent.futures.as_completed(futures):
                offer=futures[task]
                try:
                    detail=task.result()
                    if relevance(detail['title'],product):
                        for k in ['description','image','availability','native_amount','currency','native_price','model','extraction']:
                            if detail.get(k) not in [None,'']:offer[k]=detail[k]
                        offer['verified_at']=now();offer['source_kind']='Amazon product page'
                except Exception:pass
    if not offers and warnings:raise AppError('Search providers and retailer pages are unavailable right now. Please try later or connect a product-data API.',502)
    offers.sort(key=lambda o:(o.get('match')!='original product',o.get('region_code',''),o.get('native_amount') is None))
    offers=convert_offers(offers,warnings)
    if len({o['region_code'] for o in offers})<2:
        warnings.insert(0,'Cross-region comparison is incomplete. Only readable listings are shown. Connect an Amazon product-data provider for dependable regional results.')
    return {'title':product['title'],'extracted':product,'offers':offers,'demo':False,'query':target_query,'provider':'SerpApi shopping search' if SERP_KEY else 'Direct Amazon regional search','message':'Real retailer listings. A model match is not a guarantee of the same variant. Stock is confirmed only when the product page states it; region means storefront/search market, not warehouse origin.','warnings':warnings,'regions':coverage}

def normalized_image(data):
    from PIL import Image, ImageOps
    try:
        raw=base64.b64decode(data.get('image',''),validate=True)
        if not raw or len(raw)>5_000_000: raise ValueError()
        with warnings.catch_warnings():
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as original:
                if original.format not in ['JPEG','PNG','WEBP'] or original.width*original.height>20_000_000: raise ValueError()
                original.load()
                image=ImageOps.exif_transpose(original).convert('RGB')
                image.thumbnail((1200,1200))
                output=io.BytesIO();image.save(output,format='JPEG',quality=80)
                if output.tell()>500_000:
                    image.thumbnail((800,800));output=io.BytesIO();image.save(output,format='JPEG',quality=65)
                if output.tell()>500_000: raise ValueError()
                return output.getvalue()
    except Exception:
        raise AppError('Upload a valid JPEG, PNG, or WebP image, up to 5 MB and 20 megapixels.',400)

def image_search(data):
    image=normalized_image(data)
    if not SERP_KEY: raise AppError('Image search needs a SerpApi API key. Link and product-name searches are available now.',503)
    boundary=secrets.token_hex(24)
    body=(f'--{boundary}\r\nContent-Disposition: form-data; name="api_key"\r\n\r\n{SERP_KEY}\r\n--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="product.jpg"\r\nContent-Type: image/jpeg\r\n\r\n'.encode()+image+f'\r\n--{boundary}--\r\n'.encode())
    try:
        request=Request('https://serpapi.com/image',data=body,headers={'Content-Type':'multipart/form-data; boundary='+boundary})
        with urlopen(request,timeout=25) as response: uploaded=json.loads(response.read(100_000))
        image_id=uploaded.get('image_id')
        if not image_id: raise ValueError()
        result=json_request('https://serpapi.com/search.json?'+urlencode({'engine':'google_lens','image_id':image_id,'api_key':SERP_KEY,'type':'products','hl':'en'}))
        if result.get('error'): raise ValueError()
    except Exception: raise AppError('Visual search could not be completed. Please retry or search by link/name.',502)
    offers=[];seen=set()
    for match in result.get('visual_matches',[])[:40]:
        link=match.get('link','');title=match.get('title','')
        if not title or urlparse(link).scheme!='https' or link in seen: continue
        seen.add(link);price=match.get('price') or {}
        value=price.get('value','') if isinstance(price,dict) else str(price)
        amount,currency=parse_price(str(value),'')
        offers.append({'title':title,'url':link,'image':match.get('thumbnail',''),'merchant':match.get('source') or urlparse(link).hostname,'region':'Region not confirmed','region_code':'other','flag':'🌐','native_amount':amount,'native_price':str(value),'currency':currency,'match':'visual match','availability':'Not confirmed','variant':'Verify model / color / size','condition':'Not confirmed','rating':None,'reviews':None,'tone':'sage','demo':False,'description':'Visual resemblance does not confirm the same model. Price and availability need checking at the retailer.','source_kind':'Google Lens product match'})
        if len(offers)>=24:break
    notices=[];offers=convert_offers(offers,notices)
    return {'title':'Your uploaded image','query':'your uploaded image','offers':offers,'demo':False,'warnings':notices,'message':'Real visual product matches. Region and stock are not confirmed unless supplied by the retailer. Your photo is sent to SerpApi for this search; Linkcart does not save the upload.'}

def cache_search(data,session):
    search_id=secrets.token_urlsafe(18)
    with CACHE_LOCK:
        for k in list(SEARCHES):
            if SEARCHES[k]['expires']<time.time(): del SEARCHES[k]
        if len(SEARCHES)>1000: raise AppError('Search capacity reached. Try again shortly.',429)
        SEARCHES[search_id]={'session':session,'expires':time.time()+900,'data':data}
    return {**data,'search_id':search_id,'service_rate':float(SERVICE_RATE),'shipping_cents':money(SHIPPING)}

def selected_offer(data,session):
    with CACHE_LOCK: search=SEARCHES.get(str(data.get('search_id','')))
    if not search or search['expires']<time.time() or search['session']!=session: raise AppError('This price quote expired. Search again before checkout.',409)
    offer=next((x for x in search['data']['offers'] if x['id']==str(data.get('offer_id'))),None)
    if not offer: raise AppError('Product offer not found.',404)
    return offer

def quote(offer,qty):
    if not offer.get('price_cents'): raise AppError('A USD price is required before checkout.')
    item=offer['price_cents']*qty
    fee=int((Decimal(item)*SERVICE_RATE).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
    shipping=money(SHIPPING)
    return {'item_cents':item,'fee_cents':fee,'shipping_cents':shipping,'total_cents':item+fee+shipping}

def serialize(row,staff=False):
    d=dict(row); d.pop('session',None); d.pop('idempotency',None); d['offer']=json.loads(d['offer']); d['customer']=json.loads(d['customer']); d['demo']=bool(d['demo'])
    if not staff: d.pop('staff_note',None)
    return d

def hmac_signature(value): return base64.b64encode(hmac.new(PAYWAY_KEY.encode(),value.encode(),hashlib.sha512).digest()).decode()

def payment_form(order):
    customer=order['customer']; name=customer['name'].split(' ',1)
    fields={'req_time':datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S'),'merchant_id':PAYWAY_ID,'tran_id':order['id'],'amount':f"{order['total_cents']/100:.2f}",'firstname':name[0],'lastname':name[1] if len(name)>1 else '', 'email':customer['email'],'phone':re.sub(r'[^0-9+]','',customer['phone']),'type':'purchase','payment_option':order['payment_method'],'return_url':base64.b64encode((BASE_URL+'/api/payway/callback').encode()).decode(),'cancel_url':BASE_URL+'/?payment=cancelled','continue_success_url':BASE_URL+'/?payment=return&order='+order['id'],'currency':'USD','return_params':json.dumps({'order_id':order['id']},separators=(',',':'))}
    sequence=['req_time','merchant_id','tran_id','amount','items','shipping','firstname','lastname','email','phone','type','payment_option','return_url','cancel_url','continue_success_url','return_deeplink','currency','custom_fields','return_params','payout','lifetime','additional_params','google_pay_token','skip_success_page']
    fields['hash']=hmac_signature(''.join(fields.get(k,'') for k in sequence))
    return {'action':PAYWAY_BASE+'/api/payment-gateway/v1/payments/purchase','fields':fields}

def mark_paid(order_id, demo=False):
    with connect() as con:
        row=con.execute('SELECT * FROM orders WHERE id=?',(order_id,)).fetchone()
        if not row: raise AppError('Order not found.',404)
        if bool(row['demo'])!=demo: raise AppError('Payment mode mismatch.',409)
        if row['status']!='pending_payment': return serialize(row)
        con.execute("UPDATE orders SET status='paid',updated_at=?,payment_ref=? WHERE id=? AND status='pending_payment'",(now(),'DEMO' if demo else order_id,order_id))
        con.execute('INSERT INTO events(order_id,created_at,action) VALUES(?,?,?)',(order_id,now(),'Demo payment simulated' if demo else 'Payment verified with PayWay'))
        return serialize(con.execute('SELECT * FROM orders WHERE id=?',(order_id,)).fetchone())

def verify_payway(row):
    if row['demo']: raise AppError('This is a demo order.',409)
    if row['status']!='pending_payment': return serialize(row)
    req_time=datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    result=json_request(PAYWAY_BASE+'/api/payment-gateway/v1/payments/check-transaction-2', {'req_time':req_time,'merchant_id':PAYWAY_ID,'tran_id':row['id'],'hash':hmac_signature(req_time+PAYWAY_ID+row['id'])})
    status=result.get('status',{}); data=result.get('data',{})
    approved=(str(status.get('code'))=='00' and status.get('tran_id')==row['id'] and str(data.get('payment_status_code'))=='0' and data.get('payment_status')=='APPROVED')
    if approved:
        if data.get('payment_currency')!='USD' or money(data.get('original_amount',-1))!=row['total_cents'] or money(data.get('refund_amount',0))!=0:
            raise AppError('Payment amount or currency did not match. Staff must review.',409)
        return mark_paid(row['id'])
    return serialize(row)

class Handler(BaseHTTPRequestHandler):
    server_version='Linkcart'
    def log_message(self,fmt,*args):
        # Avoid logging URLs / credentials / customer details.
        pass
    def session(self):
        cookie=http.cookies.SimpleCookie()
        try: cookie.load(self.headers.get('Cookie',''))
        except http.cookies.CookieError: pass
        value=cookie.get('lc_session')
        token=value.value if value else ''
        if not re.fullmatch(r'[a-f0-9]{64}',token): token=secrets.token_hex(32)
        self.session_token=token
        return token
    def send(self,status,data,ctype='application/json'):
        raw=json.dumps(data,ensure_ascii=False).encode() if ctype=='application/json' else data.encode() if isinstance(data,str) else data
        self.send_response(status); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(raw)))
        self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Referrer-Policy','no-referrer'); self.send_header('Cache-Control','no-store')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' https: data: blob:; connect-src 'self'; form-action 'self' https://checkout.payway.com.kh https://checkout-sandbox.payway.com.kh; base-uri 'self'; frame-ancestors 'none'")
        if hasattr(self,'session_token'):
            self.send_header('Set-Cookie','lc_session='+self.session_token+'; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000'+('; Secure' if BASE_URL.startswith('https:') else ''))
        self.end_headers(); self.wfile.write(raw)
    def body(self):
        size=int(self.headers.get('Content-Length',0))
        if size>(7_000_000 if urlparse(self.path).path=='/api/search/image' else 20000): raise AppError('Request too large.',413)
        try:
            data=json.loads(self.rfile.read(size))
            if not isinstance(data,dict): raise ValueError()
            return data
        except (ValueError,UnicodeDecodeError): raise AppError('Invalid JSON request.')
    def check_origin(self):
        origin=self.headers.get('Origin')
        if origin and origin not in ({BASE_URL} | deployment_origins(os.environ)): raise AppError('Request origin is not allowed.',403)
        if self.headers.get('Sec-Fetch-Site')=='cross-site': raise AppError('Cross-site requests are not allowed.',403)
    def staff(self):
        value=self.headers.get('Authorization','').removeprefix('Bearer ')
        if not ADMIN_TOKEN or not hmac.compare_digest(value,ADMIN_TOKEN): raise AppError('Invalid staff access token.',401)
    def limited(self,kind,max_requests=20):
        key=(self.client_address[0],kind)
        with CACHE_LOCK:
            bucket=RATE_BUCKETS[key]; current=time.time()
            while bucket and current-bucket[0]>60: bucket.popleft()
            if len(bucket)>=max_requests: raise AppError('Too many requests. Try again in a minute.',429)
            bucket.append(current)
    def owned(self,order_id,session):
        with connect() as con: row=con.execute('SELECT * FROM orders WHERE id=? AND session=?',(order_id,session)).fetchone()
        if not row: raise AppError('Order not found.',404)
        return row
    def handle_error(self,error):
        if isinstance(error,AppError): self.send(error.status,{'error':error.message})
        else: self.send(502,{'error':'The request could not be completed. Please try again.'})
    def do_GET(self):
        try:
            session=self.session(); path=urlparse(self.path).path
            if path=='/api/config': return self.send(200,{'demo':False,'search_provider':'api' if SERP_KEY else 'direct_amazon','demo_enabled':DEMO_ENABLED,'payment_mode':PAYMENT_MODE,'image_search_ready':False,'regions':[{'code':r[0],'name':r[1],'flag':r[2]} for r in REGIONS],'payway_ready':PAYWAY_READY,'payway_env':PAYWAY_MODE,'live_checkout_enabled':bool(ALLOW_LIVE_CHECKOUT and PAYWAY_READY and PRICING_READY and PAYMENT_MODE==PAYWAY_MODE),'service_rate':float(SERVICE_RATE),'shipping_cents':money(SHIPPING)})
            if path=='/api/catalog':
                return self.send(200,{'title':'Find your product','offers':[],'demo':False,'search_id':'','warnings':[]})
            if path=='/api/orders':
                with connect() as con: rows=con.execute('SELECT * FROM orders WHERE session=? AND demo=? ORDER BY created_at DESC',(session,int(DEMO_ENABLED))).fetchall()
                return self.send(200,{'orders':[serialize(r) for r in rows]})
            if path=='/api/admin/orders':
                self.limited('staff',30); self.staff()
                with connect() as con: rows=con.execute('SELECT * FROM orders WHERE demo=? ORDER BY created_at DESC LIMIT 500',(int(DEMO_ENABLED),)).fetchall()
                return self.send(200,{'orders':[serialize(r,True) for r in rows]})
            if path.startswith('/api/'): raise AppError('Endpoint not found.',404)
            filepath=ROOT/'static'/path.lstrip('/') if path!='/' else ROOT/'static/index.html'
            filepath=filepath.resolve()
            if ROOT/'static' not in filepath.parents or not filepath.is_file(): raise AppError('Page not found.',404)
            return self.send(200,filepath.read_bytes(),mimetypes.guess_type(filepath)[0] or 'application/octet-stream')
        except Exception as e: self.handle_error(e)
    def do_POST(self):
        try:
            path=urlparse(self.path).path
            if path=='/api/payway/callback':
                if not PAYWAY_READY: raise AppError('Payments are not configured.',503)
                self.limited('callback',60); data=self.body()
                def php_value(value):
                    if isinstance(value,(dict,list)): return json.dumps(value,ensure_ascii=True,separators=(',',':')).replace('/','\\/')
                    if value is None or value is False: return ''
                    if value is True: return '1'
                    return str(value)
                expected=hmac_signature(''.join(php_value(data[k]) for k in sorted(data)))
                if not hmac.compare_digest(expected,self.headers.get('X-PayWay-HMAC-SHA512','')): raise AppError('Invalid callback signature.',401)
                with connect() as con: row=con.execute('SELECT * FROM orders WHERE id=?',(str(data.get('tran_id','')),)).fetchone()
                if not row: raise AppError('Order not found.',404)
                verify_payway(row)  # Do not trust callback status alone.
                return self.send(200,{'received':True})
            self.check_origin(); session=self.session(); data=self.body()
            if path=='/api/search/image':
                self.limited('search',8)
                raise AppError('Image search is currently disabled. Search by product link or name.',403)
            if path=='/api/search':
                self.limited('search',8); query=str(data.get('query','')).strip()
                if not query or len(query)>2000: raise AppError('Enter a product URL or name.')
                if query.startswith(('http://','https://')): allowed_url(query)
                if data.get('demo') is True: raise AppError('Sample product comparisons have been removed.',410)
                result=live_search(query)
                return self.send(200,cache_search(result,session))
            if path=='/api/product':
                self.limited('product',20)
                offer=selected_offer(data,session)
                if not offer.get('details_loaded'):
                    try:
                        detail=extract_product(offer['url'])
                        if offer.get('asin') and detail.get('asin')!=offer['asin']: raise ValueError()
                        merged={**offer,**{k:detail[k] for k in ['title','description','image','model','brand','features','specifications','options','availability','native_amount','native_price','currency'] if detail.get(k) not in [None,'',[],{}]}}
                        merged['details_loaded']=True;merged['verified_at']=now()
                        convert_offers([merged],[]);merged['id']=offer['id']
                        with CACHE_LOCK: offer.update(merged)
                    except Exception:
                        return self.send(200,{'product':offer,'notice':'The retailer blocked detailed information. These are search-page details; verify the variant at the retailer.'})
                return self.send(200,{'product':offer,'notice':''})
            if path=='/api/quote':
                offer=selected_offer(data,session); qty=data.get('quantity',1)
                if type(qty)!=int or qty<1 or qty>5: raise AppError('Choose a quantity between 1 and 5.')
                return self.send(200,quote(offer,qty))
            if path=='/api/checkout':
                self.limited('checkout',10)
                idem=str(data.get('idempotency',''))
                if not re.fullmatch(r'[a-f0-9-]{20,64}',idem): raise AppError('Invalid checkout request identifier.')
                with connect() as con: existing=con.execute('SELECT * FROM orders WHERE session=? AND idempotency=?',(session,idem)).fetchone()
                if existing:
                    if bool(existing['demo'])!=DEMO_ENABLED: raise AppError('Checkout belongs to another payment environment.',409)
                    order=serialize(existing)
                    return self.send(200,{'order':order,'payment':None if order['demo'] or order['status']!='pending_payment' else payment_form(order)})
                offer=selected_offer(data,session)
                if not DEMO_ENABLED and not (ALLOW_LIVE_CHECKOUT and PAYWAY_READY and PRICING_READY and PAYMENT_MODE == PAYWAY_MODE): raise AppError('Live payments and business pricing are not configured.',503)
                if not offer.get('checkout_available'): raise AppError('Live checkout is not enabled for this listing. The team must configure and verify pricing first.',409)
                qty=data.get('quantity',1)
                if type(qty)!=int or not 1<=qty<=5: raise AppError('Choose a quantity between 1 and 5.')
                customer=data.get('customer',{})
                if not isinstance(customer,dict): raise AppError('Customer details are required.')
                for field,maxlen in [('name',100),('email',50),('phone',20),('address',500)]:
                    value=customer.get(field)
                    if not isinstance(value,str) or not value.strip() or len(value)>maxlen: raise AppError('Enter a valid '+field+'.')
                if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',customer['email']): raise AppError('Enter a valid email address.')
                if not re.fullmatch(r'[+0-9 ()-]{7,20}',customer['phone']): raise AppError('Enter a valid phone number.')
                customer={k:customer[k].strip() for k in ['name','email','phone','address']}
                if data.get('consent') is not True: raise AppError('Please confirm the variant and costs before proceeding.')
                method=str(data.get('payment_method','abapay_khqr'))
                if method not in ['abapay_khqr','cards']: raise AppError('Choose KHQR or credit card.')
                variant=str(data.get('variant','')).strip()[:250]
                if not variant: raise AppError('Please specify color, size, or model.')
                costs=quote(offer,qty)
                if costs['total_cents']>500000 and not offer['demo']: raise AppError('This amount requires staff review before payment.')
                order_id='LC'+secrets.token_hex(8).upper(); stamp=now()
                values=(order_id,session,idem,stamp,stamp,'pending_payment',int(DEMO_ENABLED),json.dumps(offer),json.dumps(customer),qty,variant,method,costs['item_cents'],costs['fee_cents'],costs['shipping_cents'],costs['total_cents'])
                with connect() as con:
                    try:
                        con.execute('INSERT INTO orders(id,session,idempotency,created_at,updated_at,status,demo,offer,customer,quantity,variant,payment_method,item_cents,fee_cents,shipping_cents,total_cents) VALUES('+','.join(['?']*16)+')',values)
                        con.execute('INSERT INTO events(order_id,created_at,action) VALUES(?,?,?)',(order_id,stamp,'Checkout created'))
                    except sqlite3.IntegrityError:
                        pass
                    row=con.execute('SELECT * FROM orders WHERE session=? AND idempotency=?',(session,idem)).fetchone()
                order=serialize(row)
                return self.send(201,{'order':order,'payment':None if order['demo'] else payment_form(order)})
            if path=='/api/demo/pay':
                self.limited('demo-pay',10); row=self.owned(str(data.get('order_id','')),session)
                if not DEMO_ENABLED or not row['demo']: raise AppError('Demo payments are disabled for this order.',403)
                return self.send(200,{'order':mark_paid(row['id'],True)})
            if path=='/api/payway/verify':
                if not PAYWAY_READY: raise AppError('PayWay is not configured.',503)
                self.limited('verify',15); row=self.owned(str(data.get('order_id','')),session)
                return self.send(200,{'order':verify_payway(row)})
            if path=='/api/payway/resume':
                self.limited('resume',10); row=self.owned(str(data.get('order_id','')),session)
                if not PAYWAY_READY or PAYMENT_MODE!=PAYWAY_MODE or row['demo'] or row['status']!='pending_payment': raise AppError('This order does not need a PayWay checkout.',409)
                return self.send(200,{'payment':payment_form(serialize(row))})
            if path=='/api/admin/update':
                self.limited('staff-update',30); self.staff(); order_id=str(data.get('order_id','')); new_status=str(data.get('status',''))
                with connect() as con:
                    row=con.execute('SELECT * FROM orders WHERE id=?',(order_id,)).fetchone()
                    if not row: raise AppError('Order not found.',404)
                    if bool(row['demo'])!=DEMO_ENABLED: raise AppError('Order belongs to a different payment environment.',409)
                    current=row['status']; allowed={'pending_payment':[],'paid':['purchased'],'purchased':['shipped'],'shipped':['delivered'],'delivered':[]}
                    if new_status!=current and new_status not in allowed.get(current,[]): raise AppError('Status must advance one step after verified payment.',409)
                    tracking=str(data.get('tracking',row['tracking'])).strip()[:250]; note=str(data.get('note',row['staff_note'])).strip()[:1000]
                    if new_status=='shipped' and not tracking: raise AppError('Add a carrier and tracking number before marking shipped.')
                    con.execute('UPDATE orders SET status=?,tracking=?,staff_note=?,updated_at=? WHERE id=? AND status=?',(new_status,tracking,note,now(),order_id,current))
                    con.execute('INSERT INTO events(order_id,created_at,action) VALUES(?,?,?)',(order_id,now(),'Staff update: '+new_status))
                return self.send(200,{'updated':True})
            raise AppError('Endpoint not found.',404)
        except Exception as e: self.handle_error(e)

if __name__=='__main__':
    init_db()
    print(f'Linkcart: {BASE_URL}',flush=True)
    print('Search: '+('live API' if SERP_KEY else 'direct live Amazon regional pages'),flush=True)
    print('Payments: '+(PAYWAY_MODE if PAYWAY_READY else 'mock checkout; no money collected'),flush=True)
    print('Staff dashboard: '+('configured' if ADMIN_TOKEN else 'disabled until ADMIN_TOKEN is set'),flush=True)
    ThreadingHTTPServer((os.environ.get('HOST','127.0.0.1'),PORT),Handler).serve_forever()
