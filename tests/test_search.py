import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import unittest
from unittest.mock import patch
import server
from product_search import parse_product,parse_amazon_results,parse_price,relevance,search_query

AMAZON='''<html><head><title>Amazon product</title></head><body>
<span id="productTitle">Sony WH-1000XM5 Wireless Headphones, Black</span>
<img id="landingImage" src="https://m.media-amazon.com/images/I/example.jpg">
<div id="corePriceDisplay_desktop_feature_div"><span class="a-price a-text-price"><span class="a-offscreen">$399.99</span></span><span class="a-price"><span class="a-offscreen">$249.99</span></span></div>
<div id="feature-bullets"><ul><li>Noise cancellation</li><li>30-hour battery</li></ul></div>
<div id="availability"><span>In Stock</span></div></body></html>'''
RESULTS='''<div data-component-type="s-search-result" data-asin="B09XS7JWHH"><h2>Sony</h2><h2>WH-1000XM5 Wireless Headphones</h2><img class="s-image" src="https://m.media-amazon.com/a.jpg"><span class="a-price"><span class="a-offscreen">KHR 1,000,000.00</span></span></div>
<div data-component-type="s-search-result" data-asin="B012345678"><h2>Replacement Ear Pads for Sony WH-1000XM5</h2><span class="a-price"><span class="a-offscreen">$12.00</span></span></div>
<div data-component-type="s-search-result" data-asin="B012345679"><h2>Sony WH-1000XM5用イヤーパッド交換部品</h2><span class="a-price"><span class="a-offscreen">¥1,999</span></span></div>
<div data-component-type="s-search-result" data-asin="B012345670"><h2>Sony WH-1000XM5 Wireless Headphones, Silver</h2></div>'''
PRODUCT={'title':'Sony WH-1000XM5 Wireless Headphones','model':'WH-1000XM5','url':'https://www.amazon.com/dp/B09XS7JWHH'}

class SearchTests(unittest.TestCase):
    def test_extract_actual_product_content_and_sale_price(self):
        p=parse_product(AMAZON,PRODUCT['url'])
        self.assertEqual(p['title'],'Sony WH-1000XM5 Wireless Headphones, Black')
        self.assertEqual(p['native_amount'],249.99);self.assertEqual(p['currency'],'USD')
        self.assertEqual(p['availability'],'In stock');self.assertIn('30-hour battery',p['description'])
        self.assertTrue(p['image'].startswith('https://'));self.assertEqual(p['model'],'WH-1000XM5')
    def test_structured_product_on_another_retailer(self):
        markup='''<script type="application/ld+json">{"@graph":[{"@type":"Product","name":"Example Camera ZX-500","brand":{"name":"Example"},"mpn":"ZX-500","image":["https://images.example.com/camera.jpg"],"offers":{"price":"199.95","priceCurrency":"EUR","availability":"https://schema.org/OutOfStock"}}]}</script>'''
        p=parse_product(markup,'https://www.ebay.com/itm/123456')
        self.assertEqual(p['currency'],'EUR');self.assertEqual(p['availability'],'Out of stock')
        self.assertEqual(p['model'],'ZX-500');self.assertEqual(search_query(p),'Example ZX-500')
    def test_challenge_page_is_not_a_product(self):
        for markup in ['<title>Robot Check</title>','<title>Just a moment...</title>','<title>&nbsp;</title>']:
            with self.assertRaises(ValueError):parse_product(markup,PRODUCT['url'])
    def test_search_uses_product_heading_and_filters_accessories(self):
        result=parse_amazon_results(RESULTS,'amazon.com',server.REGIONS[0],PRODUCT)
        self.assertEqual(len(result),2);self.assertEqual(result[0]['title'],'Sony WH-1000XM5 Wireless Headphones')
        self.assertEqual(result[0]['currency'],'KHR');self.assertEqual(result[0]['native_amount'],1000000)
        self.assertEqual(result[0]['availability'],'Not confirmed');self.assertIsNone(result[1]['native_amount'])
        self.assertFalse(result[0]['checkout_available'])
    def test_price_formats_and_missing_price(self):
        for text,currency,amount in [('$249.99','USD',249.99),('£199.00','GBP',199),('€249,99','EUR',249.99),('€1.249,99','EUR',1249.99),('¥39,800','JPY',39800),('KHR 1,200,000.00','KHR',1200000)]:
            self.assertEqual(parse_price(text),(amount,currency))
        self.assertEqual(parse_price('See buying options'),(None,''))
    def test_do_not_convert_using_storefront_currency(self):
        offers=[{'title':'Example','native_amount':1000000,'currency':'KHR','availability':'Not confirmed'}]
        with patch.object(server,'DEMO_ENABLED',False), patch.object(server,'json_request',return_value={'rate':.00025,'date':'2026-10-01'}):
            result=server.convert_offers(offers,[])
        self.assertEqual(result[0]['price_cents'],25000);self.assertEqual(result[0]['fx_date'],'2026-10-01')
        self.assertFalse(result[0]['checkout_available'])
    def test_exchange_failure_keeps_native_price_without_guessing(self):
        warnings=[]
        with patch.object(server,'json_request',side_effect=OSError('offline')):
            result=server.convert_offers([{'title':'Example','native_amount':30000,'currency':'JPY'}],warnings)
        self.assertIsNone(result[0]['price_cents']);self.assertTrue(warnings)
    def test_accessory_relevance_and_alternate_models(self):
        self.assertEqual(relevance('Replacement cable for Sony WH-1000XM5',PRODUCT),0)
        self.assertEqual(relevance('Sony WH-1000XM5 ケースカバー',PRODUCT),0)
        self.assertEqual(relevance('Sony WH-1000XM6 Wireless Headphones',PRODUCT),1)
        self.assertEqual(relevance('Sony WH-1000XM5 Wireless Headphones',PRODUCT),2)
        self.assertEqual(relevance('Unrelated kitchen table',PRODUCT),0)
    def test_no_generated_offers_when_regions_are_blocked(self):
        with patch.object(server,'extract_product',return_value={'**unused**':0,**PRODUCT,'native_amount':None,'currency':'','native_price':'','availability':'Not confirmed','extraction':'Page metadata'}),patch.object(server,'direct_amazon_region',side_effect=ValueError('blocked')):
            result=server.live_search(PRODUCT['url'])
        self.assertFalse(result['demo']);self.assertEqual(len(result['offers']),1)
        self.assertIsNone(result['offers'][0]['price_cents']);self.assertEqual(len(result['warnings']),4)
    def test_live_search_returns_verified_original_and_real_matches(self):
        def region_results(product,region):
            if region[0]=='us':return parse_amazon_results(RESULTS,'amazon.com',region,PRODUCT)
            raise ValueError('blocked')
        with patch.object(server,'extract_product',return_value=parse_product(AMAZON,PRODUCT['url'])),patch.object(server,'direct_amazon_region',side_effect=region_results),patch.object(server,'json_request',return_value={'rate':.00025,'date':'2026-10-01'}),patch.object(server,'SERP_KEY',''):
            result=server.live_search(PRODUCT['url'])
        self.assertFalse(result['demo']);self.assertEqual(result['offers'][0]['match'],'original product')
        self.assertEqual(len(result['offers']),2);self.assertEqual(result['offers'][0]['price_cents'],24999)

if __name__=='__main__':unittest.main()
