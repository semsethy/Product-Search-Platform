"""Read public retailer HTML. No generated prices and no anti-bot bypasses."""
import html
import json
import math
import re
from html.parser import HTMLParser
from amazon_regions import marketplace
from urllib.parse import urljoin, urlparse, unquote

class Node:
    def __init__(self, tag='', attrs=None, parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs or []), parent
        self.children=[]; self.parts=[]
    def walk(self):
        yield self
        for child in self.children: yield from child.walk()
    def text(self):
        return ' '.join(' '.join(self.parts).split())
    def classes(self): return set(self.attrs.get('class','').split())
    def first(self, predicate): return next((n for n in self.walk() if predicate(n)),None)

class Page(HTMLParser):
    VOID={'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}
    def __init__(self,markup):
        super().__init__(convert_charrefs=True); self.root=Node(); self.stack=[self.root]; self.meta={}; self.json_blocks=[]
        self.feed(markup)
    def handle_starttag(self,tag,attrs):
        node=Node(tag,attrs,self.stack[-1]);self.stack[-1].children.append(node)
        if tag=='meta':self.meta[node.attrs.get('property',node.attrs.get('name',''))]=node.attrs.get('content','')
        if tag not in self.VOID:self.stack.append(node)
    def handle_startendtag(self,tag,attrs):
        self.handle_starttag(tag,attrs)
        if tag not in self.VOID:self.handle_endtag(tag)
    def handle_data(self,data):
        if self.stack[-1].tag in ['script','style']:
            self.stack[-1].parts.append(data);return
        for node in self.stack: node.parts.append(data)
    def handle_endtag(self,tag):
        for i in range(len(self.stack)-1,0,-1):
            if self.stack[i].tag==tag:
                self.stack=self.stack[:i];return
    def by_id(self,id):return self.root.first(lambda n:n.attrs.get('id')==id)
    def structured_product(self):
        def walk(value):
            if isinstance(value,list):
                for child in value:
                    found=walk(child)
                    if found:return found
            if isinstance(value,dict):
                t=value.get('@type',[])
                if t=='Product' or isinstance(t,list) and 'Product' in t:return value
                for child in value.values():
                    if isinstance(child,(dict,list)):
                        found=walk(child)
                        if found:return found
            return None
        for n in self.root.walk():
            if n.tag=='script' and n.attrs.get('type')=='application/ld+json':
                try:
                    product=walk(json.loads(''.join(n.parts)))
                    if product:return product
                except (ValueError,RecursionError):pass
        return None

def clean(value,limit=400):
    return re.sub(r'\s+',' ',html.unescape(re.sub(r'<[^>]*>',' ',str(value or '')))).strip()[:limit]

def https_image(value,base=''):
    if isinstance(value,list):value=value[0] if value else ''
    if isinstance(value,dict):value=value.get('url','')
    value=urljoin(base,str(value or ''))
    return value if urlparse(value).scheme=='https' else ''

def asin_from_url(url):
    return (re.search(r'/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?:[/?]|$)',url,re.I).group(1).upper()
            if re.search(r'/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?:[/?]|$)',url,re.I) else '')

def canonical(url):
    asin=asin_from_url(url);p=urlparse(url)
    if asin and 'amazon.' in p.hostname:return f'https://{p.hostname}/dp/{asin}'
    return p._replace(fragment='').geturl()

def infer_model(title):
    matches=re.findall(r'\b[A-Za-z]{1,10}[-]?[0-9][A-Za-z0-9-]{2,20}\b',title)
    return next((m.upper() for m in matches if not re.fullmatch(r'\d+(?:GB|TB|HZ|MAH|MM)',m,re.I)), '')

def normalized(value):return re.sub(r'[^a-z0-9]','',str(value).lower())

ACCESSORIES=re.compile(r'\b(?:ear\s*pads?|ear\s*cushions?|replacement\s+(?:pads?|cushions?|headbands?)|carrying\s+case|protective\s+(?:case|cover)|headphone\s+stand|compatible\s+with|for\s+sony|earpad|silicone\s+(?:case|cover)|replacement|headband\s+(?:cover|hanger)|audio\s+cable|aux\s+(?:cable|cord)|repair\s+kit|charging\s+cable|charger|cable|cord|headphone\s+cover)\b|ケース|カバー|イヤ[ー]?パッド|イヤークッション|交換|ケーブル|ヒンジ|修理|アクセサリ',re.I)
REGIONAL_ACCESSORIES=re.compile(r'earpads?|padz|ohrpolster|ersatz(?:polster|teile)|schutzhülle|coussinets?|housse|coque|remplacement|almohadillas|funda|repuesto|cuscinetti|custodia|ricambio|nauszniki|poduszki|wymienne|zawias|etui|naprawcz|oorkussens|beschermhoes|öronkuddar|fodral|almofadas|substitui|وسادات|قطع غيار|حافظة',re.I)
STOP={'the','with','for','and','from','this','that','wireless','premium','new','black','white','silver','buy','amazon','noise','cancelling','canceling','leading','industry','headphones','headphone','product','free','shipping'}

CATEGORY_PATTERNS={
 'headphones':r'headphones?|ヘッドホン|ヘッドフォン',
 'sweatshirt':r'sweatshirt|hood(?:ie|ed)|フーディ|パーカー|スウェット',
 'shoes':r'sneakers?|running shoes?|スニーカー|ランニングシューズ',
 'camera':r'camera|カメラ', 'laptop':r'laptop|notebook computer|ノートパソコン',
 'watch':r'watch|腕時計', 'backpack':r'backpack|リュック',
}
def alternative_query(product):
    category=product_category(product.get('title',''))
    brand=product.get('brand','')
    title=product.get('title','')
    if not brand and re.search(r'new balance|ニューバランス',title,re.I):brand='New Balance'
    if not brand and title:brand=title.split()[0]
    return (brand+' '+category).strip() if category else ''

def product_category(title):
    return next((name for name,pattern in CATEGORY_PATTERNS.items() if re.search(pattern,title,re.I)),'')

def relevance(title, source):
    model=str(source.get('model') or infer_model(source.get('title',''))).split('/')[0]
    if model:
        # Accessories mention the model but are not the same product category.
        if (ACCESSORIES.search(title) or REGIONAL_ACCESSORIES.search(title)) and not (ACCESSORIES.search(source.get('title','')) or REGIONAL_ACCESSORIES.search(source.get('title',''))):return 0
        if normalized(model) in normalized(title):
            comparison=re.search(r'(?:same .{0,30}as|same .{0,30}in|compared? (?:with|to)|alternative to|versus|\bvs\b)',title,re.I)
            if not comparison:return 2
            return 1 if product_category(source.get('title',''))==product_category(title) else 0
        # Nearby headphone models can be useful alternatives, but are never labeled model matches.
        category=product_category(source.get('title',''))
        if category and category==product_category(title):return 1
        return 0
    words={w for w in re.findall(r'[a-z0-9]+',source.get('title','').lower()) if len(w)>2 and w not in STOP}
    candidate=set(re.findall(r'[a-z0-9]+',title.lower()))
    if not words:return 0
    overlap=len(words&candidate)/len(words)
    return 1 if overlap>=.5 else 0

def search_query(product):
    model=str(product.get('model') or infer_model(product.get('title',''))).split('/')[0]
    title=product.get('title','')
    if model:
        brand=product.get('brand','') or ('New Balance' if title.lower().startswith('new balance ') else (title.split()[0] if title else ''))
        return ' '.join(dict.fromkeys([brand,model])).strip()
    title=re.sub(r'\s*[:|]\s*(?:Amazon|eBay|Walmart).*$', '',title,flags=re.I)
    return ' '.join(title.split()[:10])

def parse_price(text, fallback=''):
    text=clean(text)
    currencies=[('MX$','MXN'),('R$','BRL'),('S$','SGD'),('CA$','CAD'),('AU$','AUD'),('AED','AED'),('SAR','SAR'),('EGP','EGP'),('ZAR','ZAR'),('SEK','SEK'),('PLN','PLN'),('TRY','TRY'),('MXN','MXN'),('BRL','BRL'),('INR','INR'),('₹','INR'),('zł','PLN'),('US$','USD'),('USD','USD'),('KHR','KHR'),('£','GBP'),('GBP','GBP'),('€','EUR'),('EUR','EUR'),('￥','JPY'),('¥','JPY'),('JPY','JPY'),('A$','AUD'),('C$','CAD'),('SGD','SGD'),('$','USD')]
    currency=next((code for symbol,code in currencies if symbol in text),fallback)
    if currency=='USD' and '$' in text and not any(s in text for s in ['US$','USD','A$','C$','S$','MX$','R$']) and fallback in ['CAD','AUD','SGD','MXN']:currency=fallback
    raw=re.search(r'\d[\d\s.,]*',text)
    if not raw or not currency:return None,currency
    value=re.sub(r'\s+','',raw.group()).strip('.,')
    if ',' in value and '.' in value:
        value=value.replace('.','').replace(',','.') if value.rfind(',')>value.rfind('.') else value.replace(',','')
    elif ',' in value:
        value=value.replace(',','.') if re.search(r',\d{2}$',value) and currency not in ['JPY','KHR'] else value.replace(',','')
    try:
        amount=float(value)
        return (amount if amount>0 and math.isfinite(amount) else None),currency
    except ValueError:return None,currency

def current_price(node):
    for n in node.walk():
        if 'a-price' not in n.classes() or 'a-text-price' in n.classes():continue
        text=n.first(lambda child:'a-offscreen' in child.classes())
        if text and text.text():return text.text()
    return ''

def parse_product(markup,url):
    page=Page(markup);structured=page.structured_product();asin=asin_from_url(url)
    out={'url':canonical(url),'asin':asin,'model':'','brand':'','image':'','description':'','native_amount':None,'currency':'','native_price':'','availability':'Not confirmed','extraction':'','title':''}
    if structured and structured.get('name'):
        out['title']=clean(structured['name']);out['model']=clean(structured.get('mpn') or structured.get('model'),80)
        brand=structured.get('brand',{});out['brand']=clean(brand.get('name') if isinstance(brand,dict) else brand,80)
        out['image']=https_image(structured.get('image'),url);out['description']=clean(structured.get('description'),1500)
        offers=structured.get('offers',{});offers=offers[0] if isinstance(offers,list) and offers else offers
        if isinstance(offers,dict):
            raw=offers.get('price') or offers.get('lowPrice');curr=offers.get('priceCurrency','')
            out['native_amount'],out['currency']=parse_price(str(raw or ''),curr)
            out['native_price']=f'{curr} {raw}' if out['native_amount'] else ''
            stock=str(offers.get('availability','')).split('/')[-1]
            out['availability']={'InStock':'In stock','OutOfStock':'Out of stock','PreOrder':'Preorder','LimitedAvailability':'Limited availability'}.get(stock,'Not confirmed')
        out['extraction']='Structured product data'
    if asin and 'amazon.' in urlparse(url).hostname:
        title=page.by_id('productTitle')
        if title and title.text():out['title']=clean(title.text())
        image=page.by_id('landingImage') or page.by_id('imgBlkFront')
        if image:
            out['image']=https_image(image.attrs.get('src') or image.attrs.get('data-old-hires'),url)
            if not out['image']:
                try:out['image']=https_image(next(iter(json.loads(image.attrs.get('data-a-dynamic-image','{}')))),url)
                except (ValueError,StopIteration):pass
        for id in ['corePriceDisplay_desktop_feature_div','corePrice_feature_div','corePrice_desktop','apex_desktop','priceblock_ourprice','priceblock_dealprice']:
            node=page.by_id(id)
            if node:
                price=current_price(node) or (node.text() if id.startswith('priceblock') else '')
                amount,currency=parse_price(price,(marketplace(urlparse(url).hostname) or ('','','',''))[3])
                if amount:out.update(native_amount=amount,currency=currency,native_price=price);break
        byline=page.by_id('bylineInfo')
        if byline:
            brand=clean(byline.text(),100)
            brand=re.sub(r'^(?:Visit the|Brand:|ブランド[:：]?)\s*','',brand,flags=re.I)
            brand=re.sub(r'(?:\s+Store|\s*の?ストアを表示)$','',brand,flags=re.I).strip()
            if re.search(r'new balance|ニューバランス',brand,re.I):brand='New Balance'
            out['brand']=brand
        bullets=page.by_id('feature-bullets')
        if bullets:out['description']=clean(bullets.text(),1500)
        availability=page.by_id('availability')
        if availability:
            text=availability.text().lower()
            if any(w in text for w in ['currently unavailable','out of stock','derzeit nicht verfügbar','一時的に在庫切れ']):out['availability']='Out of stock'
            elif any(w in text for w in ['in stock','auf lager','在庫あり']):out['availability']='In stock'
        if title and title.text():out['extraction']='Amazon product page'
    if 'amazon.' in (urlparse(url).hostname or '') and (not asin or out['extraction']!='Amazon product page'):
        raise ValueError('Amazon did not return the requested product detail page.')
    if not out['title']:
        title=page.meta.get('og:title','');doc_title=page.root.first(lambda n:n.tag=='title')
        if not title and doc_title:title=doc_title.text()
        title=clean(title)
        if title and not any(w in title.lower() for w in ['captcha','robot check','access denied','just a moment','sorry!','amazon.com :','amazon.co.jp :','verify you are','page not found','amazon.co.uk :','amazon.de :']):
            out['title']=title;out['extraction']='Page metadata; product details need verification'
        out['image']=out['image'] or https_image(page.meta.get('og:image'),url)
        out['description']=out['description'] or clean(page.meta.get('og:description') or page.meta.get('description'),1500)
        price=page.meta.get('product:price:amount') or page.meta.get('og:price:amount')
        currency=page.meta.get('product:price:currency') or page.meta.get('og:price:currency')
        if price and currency:
            out['native_amount'],out['currency']=parse_price(price,currency);out['native_price']=currency+' '+price
    if not out['title']:raise ValueError('The retailer did not return a readable product page.')
    out['features']=[];out['specifications']={};out['options']={}
    bullets=page.by_id('feature-bullets')
    if bullets:out['features']=[clean(n.text(),350) for n in bullets.walk() if n.tag=='li' and n.text()][:12]
    for table_id in ['productDetails_techSpec_section_1','productDetails_detailBullets_sections1']:
        table=page.by_id(table_id)
        if table:
            for row in table.walk():
                if row.tag!='tr':continue
                cells=[n for n in row.children if n.tag in ['th','td']]
                if len(cells)>=2:out['specifications'][clean(cells[0].text(),100)]=clean(cells[1].text(),300)
    for option_id,label in [('variation_color_name','Color'),('variation_size_name','Size'),('variation_style_name','Style')]:
        node=page.by_id(option_id)
        if not node:continue
        values=[]
        for item in node.walk():
            value=item.attrs.get('data-defaultasin') and (item.attrs.get('title') or item.text())
            if item.tag=='option':value=item.text()
            if value:
                value=clean(value,100);value=re.sub(r'^(?:Click to select|Select)\s*','',value,flags=re.I)
                if value and value.lower() not in ['select','choose','select size',label.lower()] and value not in values:values.append(value)
        if values:out['options'][label]=values[:30]
    out['model']=out['model'] or infer_model(out['title'])
    return out

def parse_amazon_results(markup,domain,region,source):
    page=Page(markup);out=[];seen=set()
    for node in page.root.walk():
        if node.attrs.get('data-component-type')!='s-search-result':continue
        asin=node.attrs.get('data-asin','')
        if not re.fullmatch(r'[A-Z0-9]{10}',asin) or asin in seen:continue
        headings=[n.text() for n in node.walk() if n.tag=='h2']
        title=clean(max(headings,key=len)) if headings else ''
        if len(headings)>1 and len(headings[0])<30 and headings[0].lower() not in title.lower():title=clean(headings[0]+' '+title)
        if not title:continue
        score=relevance(title,source)
        if not score:continue
        seen.add(asin)
        img=node.first(lambda n:n.tag=='img' and 's-image' in n.classes())
        price=current_price(node);amount,currency=parse_price(price,region[3])
        star=node.first(lambda n:'a-icon-alt' in n.classes() and ('out of' in n.text() or '5つ星' in n.text()))
        rating_match=re.search(r'(\d[.,]\d)',star.text()) if star else None
        rating=float(rating_match.group(1).replace(',','.')) if rating_match else None
        delivery=node.first(lambda n:n.attrs.get('data-cy')=='delivery-recipe')
        availability='Not confirmed'
        if delivery and re.search(r'currently unavailable|out of stock',delivery.text(),re.I):availability='Out of stock'
        out.append({'title':title,'url':f'https://www.{domain}/dp/{asin}','asin':asin,'model':infer_model(title),'image':https_image(img.attrs.get('src'), 'https://www.'+domain) if img else '', 'native_amount':amount,'currency':currency,'native_price':price,'rating':rating,'reviews':None,'availability':availability,'condition':'Verify condition','variant':'Verify exact color / size','match':'model match' if score==2 else 'similar listing','merchant':'Amazon '+region[1], 'region_code':region[0], 'region':region[1], 'flag':region[2], 'demo':False,'art':'','color':'','tone':'sage','checkout_available':False,'description':'Listing discovered on Amazon’s live search page. Stock, shipping to Cambodia, exact variant, seller, and region-specific specifications need verification.', 'source_kind':'Amazon live search','verified_at':'','extraction':'Amazon search result'})
    if not out and not any(n.attrs.get('data-component-type')=='s-search-result' for n in page.root.walk()):
        raise ValueError('Amazon returned no readable product search results; it may be blocking this request.')
    return out[:8]
